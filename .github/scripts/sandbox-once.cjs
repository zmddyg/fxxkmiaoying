'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { setTimeout: sleep } = require('node:timers/promises');
const { JobError, createApi, makeAgent } = require('../../function/miaoying');
const { assertActivity, buildSchedule, runStage, statusOf } = require('../../function/workflow');

function log(value) {
    console.log(JSON.stringify(value));
}

async function main() {
    const startedAt = Date.now();
    const scheduledAt = Date.parse(process.env.EXPECTED_SCHEDULE_UTC);
    const expectedOpen = Date.parse(process.env.EXPECTED_OPEN_UTC);
    if (!Number.isFinite(scheduledAt) || !Number.isFinite(expectedOpen)
        || startedAt < scheduledAt - 60000 || startedAt >= expectedOpen + 20 * 60000) {
        throw new JobError('outside_one_time_test');
    }
    const plan = JSON.parse(Buffer.from(process.env.MIAOYING_REHEARSAL_PLAN || '', 'base64').toString('utf8'));
    const token = process.env.MIAOYING_REHEARSAL_TOKEN;
    const [item] = plan.occurrences || [];
    if (plan.occurrences.length !== 1 || item.opens !== expectedOpen
        || item.closes !== expectedOpen + 20 * 60000 || plan.activityRules._id !== plan.payload.tongjiId
        || plan.activityRules.createdBy !== plan.payload.userId
        || !plan.activityRules.title.endsWith('课堂签到仿真')) {
        throw new JobError('test_target_unverified');
    }
    const { occurrences } = buildSchedule(plan);
    const slot = occurrences.get(item.day + '/' + item.slot);
    const photo = fs.readFileSync(path.join(__dirname, '../../function/black.jpg'));
    const agent = makeAgent();
    let state = null;
    let submissions = 0;
    const ledger = {
        async get() { return state; },
        async add(current, contents) {
            if (state) throw new JobError('test_state_already_exists');
            state = { _id: current.key, photo: current.photo, ...contents };
        },
        async move(current, conditions, contents) {
            if (!state || state._id !== current.key
                || Object.entries(conditions).some(([key, value]) => state[key] !== value)) return false;
            state = { ...state, ...contents };
            return true;
        },
    };
    const originalApi = createApi(plan, token, agent, photo);
    const durations = {};
    const api = Object.fromEntries(Object.entries(originalApi).map(([name, action]) => [name, async (...args) => {
        const start = performance.now();
        try {
            if (name === 'submit') submissions++;
            const result = await action(...args);
            if (name === 'snapshot') {
                assertActivity(plan, result.activity);
                if (result.activity.createdBy !== plan.payload.userId || result.activity.publishResult !== false) {
                    throw new JobError('test_target_unverified');
                }
            }
            return result;
        } finally {
            (durations[name] ??= []).push(Math.round(performance.now() - start));
        }
    }]));

    async function stage(offset) {
        const start = performance.now();
        const timings = {};
        const budget = { ms(maximum = Infinity) {
            const available = Math.floor(Math.min(maximum, start + 2700 - performance.now()));
            if (available < 75) throw new JobError('time_budget_exhausted');
            return available;
        } };
        let status;
        try {
            status = await runStage(slot, offset, { plan, api, ledger, budget, now: Date.now,
                timings, photoBytes: photo.length });
        } catch (error) {
            status = statusOf(error);
        }
        log({ kind: 'stage', at: new Date().toISOString(), offset, status,
            total_ms: Math.round(performance.now() - start), ...timings });
        return status;
    }

    try {
        log({ kind: 'start', event: process.env.GITHUB_EVENT_NAME, at: new Date(startedAt).toISOString(),
            scheduled_at: process.env.EXPECTED_SCHEDULE_UTC,
            ...(process.env.GITHUB_EVENT_NAME === 'schedule' ? { script_delay_ms: startedAt - scheduledAt } : {}),
            photo_bytes: photo.length });
        if (process.env.GITHUB_EVENT_NAME === 'schedule') {
            const start = performance.now();
            const budget = { ms(maximum = Infinity) {
                const available = Math.floor(Math.min(maximum, start + 2700 - performance.now()));
                if (available < 75) throw new JobError('time_budget_exhausted');
                return available;
            } };
            const snapshot = await api.snapshot(slot, budget);
            const fieldId = plan.payload.formVal[plan.photoIndex].id;
            const [row] = snapshot.rows;
            const verified = snapshot.rows.length === 1 && row.isRemove !== true && row.isPassSecurity === true
                && row.formVal?.some((field) => field.id === fieldId && field.value === slot.photo)
                && row.createdAt >= slot.opens && row.createdAt < slot.closes;
            log({ kind: 'result', status: verified ? 'scheduled_receipt_verified' : 'scheduled_receipt_unconfirmed',
                verified, submission_calls: 0, total_ms: Math.round(performance.now() - start), api_ms: durations });
            if (!verified) process.exitCode = 1;
            return;
        }
        for (let attempt = 0; attempt < 8 && state?.phase !== 'ready' && Date.now() < slot.closes; attempt++) {
            const status = await stage(-1);
            if (['test_target_unverified', 'activity_rules_changed', 'reauthorization_required',
                'existing_submitted', 'existing_record_persisted_review_unconfirmed'].includes(status)) break;
            if (state?.phase !== 'ready') await sleep(1500);
        }
        if (state?.phase !== 'ready') throw new JobError('photo_preparation_failed');
        const remaining = slot.opens - Date.now();
        if (remaining > 0) {
            log({ kind: 'waiting_for_open', seconds: Math.ceil(remaining / 1000) });
            await sleep(remaining);
        }
        let status = 'receipt_pending';
        for (let attempt = 0; attempt < 10 && Date.now() < slot.closes; attempt++) {
            const elapsedMinutes = Math.max(0, Math.floor((Date.now() - slot.opens) / 60000));
            const offset = attempt === 0 ? elapsedMinutes : Math.max(1, elapsedMinutes);
            status = await stage(offset);
            if (state?.phase === 'verified') break;
            if (['test_target_unverified', 'activity_rules_changed', 'reauthorization_required',
                'receipt_does_not_match', 'multiple_records', 'previous_record_removed'].includes(status)) break;
            await sleep(3000);
        }
        log({ kind: 'result', status, verified: state?.phase === 'verified',
            submission_calls: submissions, api_ms: durations });
        if (state?.phase !== 'verified') process.exitCode = 1;
    } finally {
        agent.destroy();
    }
}

main().catch((error) => {
    log({ kind: 'result', status: statusOf(error), verified: false });
    process.exitCode = 1;
});
