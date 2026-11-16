'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { runJob, selectSlot } = require('./runner');
const fixture = require('../tests/plan.example.json');

test('selects the current or upcoming window and can revisit a recent receipt', () => {
    const slot = fixture.occurrences[0];
    assert.equal(selectSlot(fixture, slot.opens - 11 * 60000), undefined);
    assert.equal(selectSlot(fixture, slot.opens - 10 * 60000).day, slot.day);
    assert.equal(selectSlot(fixture, slot.opens + 1000).day, slot.day);
    assert.equal(selectSlot(fixture, slot.closes + 14 * 60000).day, slot.day);
    assert.equal(selectSlot(fixture, slot.closes + 15 * 60000), undefined);
});

test('a previous dispatch is reconciled without a second submit', async () => {
    const plan = structuredClone(fixture);
    const item = plan.occurrences[0];
    let clock = item.opens + 1000;
    let submissions = 0;
    let state = { _id: item.key, phase: 'dispatching', claim: 'prior-claim', photo: item.photo,
        photoField: plan.payload.formVal[plan.photoIndex].id };
    const ledger = {
        async get() { return structuredClone(state); },
        async move(slot, conditions, patch) {
            assert.equal(slot.key, state._id);
            if (Object.entries(conditions).some(([key, value]) => state[key] !== value)) return false;
            state = { ...state, ...patch };
            return true;
        },
    };
    const api = {
        async records() { return [{ _id: 'receipt', isPassSecurity: true,
            formVal: [{ id: state.photoField, value: item.photo }], createdAt: clock }]; },
        async submit() { submissions++; return true; },
    };
    const status = await runJob({ plan, api, ledger, photoBytes: 285, now: () => clock,
        wait: async (milliseconds) => { clock += milliseconds; }, log: () => {} });
    assert.equal(status, 'submitted');
    assert.equal(state.phase, 'verified');
    assert.equal(submissions, 0);
});

for (const startOffset of [-60000, 60000]) test(`prepares and submits once with start offset ${startOffset}`, async () => {
    const plan = structuredClone(fixture);
    const item = plan.occurrences[0];
    let clock = item.opens + startOffset;
    let state = null;
    let rows = [];
    let submissions = 0;
    const ledger = {
        async get() { return structuredClone(state); },
        async add(slot, contents) { state = { _id: slot.key, photo: slot.photo, ...contents }; },
        async move(slot, conditions, patch) {
            if (!state || slot.key !== state._id
                || Object.entries(conditions).some(([key, value]) => state[key] !== value)) return false;
            state = { ...state, ...patch };
            return true;
        },
    };
    const api = {
        async snapshot() { return { activity: { ...plan.activityRules, isRemove: false, isClosed: null }, rows }; },
        async sign() { return { expires: item.closes, maxSize: 30, policy: 'p', signature: 's', OSSAccessKeyId: 'k' }; },
        async upload() {},
        async register() {},
        async submit(slot) {
            submissions++;
            rows = [{ _id: 'receipt', isPassSecurity: true, formVal: slot.input.formVal, createdAt: clock }];
            return true;
        },
        async records() { return rows; },
    };
    const status = await runJob({ plan, api, ledger, photoBytes: 285, now: () => clock,
        wait: async (milliseconds) => { clock += milliseconds; }, log: () => {} });
    assert.equal(status, 'submitted');
    assert.equal(submissions, 1);
    assert.equal(state.phase, 'verified');
});
