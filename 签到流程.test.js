'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { setImmediate: nextTurn } = require('node:timers/promises');
const { JobError } = require('./function/miaoying');
const { MINUTE, buildSchedule, runStage } = require('./function/workflow');
const plan = require('./tests/plan.example.json');

const { times, occurrences } = buildSchedule(plan);
const slot = occurrences.get('2026-11-16/1');
const photoField = plan.payload.formVal[plan.photoIndex].id;

function simulation() {
    const time = { now: slot.opens };
    const state = { document: null, records: [], writes: [] };
    const ledger = {
        async get() { return structuredClone(state.document); },
        async add(item, content) {
            assert.equal(state.document, null);
            state.document = { _id: item.key, photo: item.photo, ...structuredClone(content) };
        },
        async move(item, expected, patch) {
            await nextTurn();
            if (!state.document || state.document._id !== item.key
                || Object.entries(expected).some(([k, v]) => state.document[k] !== v)) return false;
            Object.assign(state.document, structuredClone(patch));
            return true;
        },
    };
    const config = { ...plan.activityRules, isClosed: null, isRemove: false, publishResult: false };
    const api = {
        async snapshot() { return { activity: config, rows: state.records }; },
        async sign() {
            return { expires: slot.closes, maxSize: 30, policy: 'policy', signature: 'signature', OSSAccessKeyId: 'key' };
        },
        async upload(item) { state.writes.push(['upload', item.photo]); },
        async register(item) { state.writes.push(['register', item.photo]); },
        async submit(item) {
            state.writes.push(['submit', item.photo]);
            state.records.push({ _id: 'receipt', formVal: item.input.formVal, isPassSecurity: false, createdAt: time.now });
            return true;
        },
        async records() { return state.records; },
    };
    const job = { plan, api, ledger, timings: {}, budget: { ms: (maximum = 2700) => Math.min(maximum, 2700) },
        now: () => time.now, photoBytes: 285 };
    const ready = () => { state.document = { _id: slot.key, phase: 'ready', photo: slot.photo }; };
    return { time, state, job, ready };
}

test('calendar compiles the actual date and slot into a unique photo and payload', () => {
    assert.equal(times.get(slot.opens / MINUTE).slot.photo, slot.photo);
    assert.equal(slot.input.submitDayStr, slot.day);
    assert.equal(slot.input.submitDayIndex, slot.slot);
    assert.equal(slot.input.formVal[plan.photoIndex].value, slot.photo);
    assert.equal(slot.input.infoVal[plan.photoInfoIndex], slot.photo);
    assert.equal(new Set([...occurrences.values()].map((s) => s.photo)).size, occurrences.size);
});

test('prepares a distinct photo before opening and verifies acceptance in a separate invocation', async () => {
    const { time, state, job } = simulation();
    time.now = slot.opens - 6 * MINUTE;
    assert.equal(await runStage(slot, -6, job), 'photo_signed');
    time.now += MINUTE;
    assert.equal(await runStage(slot, -5, job), 'photo_uploaded');
    assert.equal(state.document.signing, null);
    time.now += MINUTE;
    assert.equal(await runStage(slot, -4, job), 'photo_ready');
    assert.deepEqual(state.writes.map(([op]) => op), ['upload', 'register']);
    time.now = slot.opens;
    assert.equal(await runStage(slot, 0, job), 'submission_acknowledged_waiting_for_receipt');
    time.now += MINUTE;
    assert.equal(await runStage(slot, 1, job), 'record_persisted_review_unconfirmed');
    state.records[0].isPassSecurity = true;
    time.now += MINUTE;
    assert.equal(await runStage(slot, 2, job), 'submitted');
    assert.equal(state.document.phase, 'verified');
    assert.equal(state.document.receiptId, 'receipt');
    assert.deepEqual(state.writes.map(([op]) => op), ['upload', 'register', 'submit']);
});

test('duplicate timer calls race on the same persistent claim', async () => {
    const { state, job, ready } = simulation();
    ready();
    const outcomes = await Promise.all(Array.from({ length: 20 }, () => runStage(slot, 0, job)));
    assert.equal(outcomes.filter((status) => status === 'submission_acknowledged_waiting_for_receipt').length, 1);
    assert.deepEqual(state.writes, [['submit', slot.photo]]);
});

test('an unprepared slot cancels the parallel read and performs no network write', async () => {
    const { state, job } = simulation();
    let cancelled = false;
    job.api.snapshot = (_slot, _budget, signal) => new Promise((resolve, reject) => {
        signal.addEventListener('abort', () => { cancelled = true; reject(new JobError('cancelled')); }, { once: true });
    });
    assert.equal(await runStage(slot, 0, job), 'photo_not_ready_or_already_claimed');
    assert.equal(cancelled, true);
    assert.deepEqual(state.writes, []);
});

test('an uncertain submission is reconciled by its exact photo, with no second write', async () => {
    const { time, state, job, ready } = simulation();
    ready();
    job.api.submit = async (item) => {
        state.writes.push(['submit', item.photo]);
        throw new JobError('transport_timeout');
    };
    assert.equal(await runStage(slot, 0, job), 'submission_outcome_unknown');
    time.now += MINUTE;
    assert.equal(await runStage(slot, 1, job), 'submission_outcome_unknown');
    state.records = [{ _id: 'receipt', isPassSecurity: true, formVal: [{ id: photoField, value: 'other.jpg' }] }];
    assert.equal(await runStage(slot, 2, job), 'receipt_does_not_match');
    state.records[0].formVal[0].value = slot.photo;
    assert.equal(await runStage(slot, 3, job), 'submitted');
    assert.deepEqual(state.writes, [['submit', slot.photo]]);
});

test('preflight failure rolls back a claim when no submit call happened', async () => {
    const { time, state, job, ready } = simulation();
    ready();
    const healthySnapshot = job.api.snapshot;
    job.api.snapshot = async () => { throw new JobError('AttendanceSnapshot_unavailable'); };
    assert.equal(await runStage(slot, 0, job), 'AttendanceSnapshot_unavailable');
    assert.equal(state.document.phase, 'ready');
    assert.equal(state.writes.length, 0);
    job.api.snapshot = healthySnapshot;
    time.now += MINUTE;
    assert.equal(await runStage(slot, 1, job), 'submission_acknowledged_waiting_for_receipt');
    assert.equal(state.writes.length, 1);
});

test('effective rule changes and existing records prevent submission', async () => {
    const { state, job, ready } = simulation();
    ready();
    const original = await job.api.snapshot();
    job.api.snapshot = async () => ({ ...original, activity: { ...original.activity, needLocation: true } });
    assert.equal(await runStage(slot, 0, job), 'activity_rules_changed');
    job.api.snapshot = async () => ({ ...original, rows: [{ _id: 'manual', isPassSecurity: true }] });
    assert.equal(await runStage(slot, 0, job), 'existing_submitted');
    assert.equal(state.writes.length, 0);
});

test('the final invocation can expire unused media credentials and does not send a late submission', async () => {
    const { time, state, job, ready } = simulation();
    ready();
    time.now = slot.closes;
    assert.equal(await runStage(slot, 22, job), 'window_closed_no_submission');
    state.document.phase = 'signed';
    state.document.signing = { token: 'test' };
    assert.equal(await runStage(slot, 22, job), 'window_closed_no_submission');
    assert.equal(state.document.signing, null);
    assert.deepEqual(state.writes, []);
});
