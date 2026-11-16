'use strict';

const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { setTimeout: sleep } = require('node:timers/promises');
const { createApi, makeAgent, JobError } = require('../function/miaoying');
const { createLedger } = require('../function/ledger');
const { assertActivity, buildSchedule, recordStatus, runStage, statusOf } = require('../function/workflow');

const MINUTE = 60000;
const LEAD = 10 * MINUTE;
const GRACE = 15 * MINUTE;
const RETRY = 15000;
const TERMINAL = new Set([
    'submitted', 'existing_submitted', 'activity_rules_changed', 'activity_closed',
    'activity_unavailable', 'reauthorization_required', 'account_mismatch',
    'multiple_records', 'previous_record_removed', 'receipt_does_not_match',
    'existing_multiple_records', 'existing_previous_record_removed',
    'existing_record_persisted_review_unconfirmed', 'prepared_photo_changed',
    'window_closed_no_submission',
]);

function selectSlot(plan, at) {
    const { occurrences } = buildSchedule(plan);
    const matches = [...occurrences.values()]
        .filter((slot) => slot.opens <= at + LEAD && slot.closes + GRACE > at)
        .sort((a, b) => a.opens - b.opens);
    return matches.find((slot) => slot.closes > at) || matches.at(-1);
}

async function runJob({ plan, api, ledger, photoBytes, now = Date.now, wait = sleep, log = console.log }) {
    const slot = selectSlot(plan, now());
    if (!slot) {
        log(JSON.stringify({ status: 'no_matching_window' }));
        return 'no_matching_window';
    }
    let status = 'unknown';
    let ready = false;
    while (now() < slot.closes + GRACE) {
        if (ready && now() < slot.opens) {
            await wait(Math.min(slot.opens - now(), 30 * 1000));
            continue;
        }
        const started = performance.now();
        const budget = { ms(maximum = Infinity) {
            const remaining = Math.floor(Math.min(maximum, started + 7000 - performance.now()));
            if (remaining < 75) throw new JobError('time_budget_exhausted');
            return remaining;
        } };
        const timings = {};
        try {
            // A positive offset after opening reconciles any existing dispatch claim first.
            const offset = now() < slot.opens ? -1 : 1;
            status = await runStage(slot, offset, { plan, api, ledger, budget, now, timings, photoBytes });
        } catch (error) {
            status = statusOf(error);
        }
        log(JSON.stringify({ status, duration_ms: Math.round(performance.now() - started), ...timings }));
        if (TERMINAL.has(status)) return status;
        if (now() >= slot.closes && ['submission_outcome_unknown', 'record_persisted_review_unconfirmed'].includes(status)) {
            return status;
        }
        ready = status === 'photo_ready';
        if (!ready) await wait(Math.min(RETRY, Math.max(1, slot.closes + GRACE - now())));
    }
    return status === 'submission_acknowledged_waiting_for_receipt' ? 'submission_outcome_unknown' : status;
}

async function main() {
    const plan = JSON.parse(Buffer.from(process.env.MIAOYING_PLAN || '', 'base64').toString('utf8'));
    const token = process.env.MIAOYING_SESSION_TOKEN;
    const photo = readFileSync(join(__dirname, '../function/black.jpg'));
    const agent = makeAgent();
    try {
        const api = createApi(plan, token, agent, photo);
        const ledger = createLedger();
        if (process.env.MIAOYING_ENABLE_SUBMIT !== '1') {
            const slots = [...buildSchedule(plan).occurrences.values()].sort((a, b) => a.opens - b.opens);
            const slot = selectSlot(plan, Date.now()) || slots.find((item) => item.closes > Date.now()) || slots.at(-1);
            if (!slot) throw new JobError('no_matching_window');
            const start = performance.now();
            const budget = { ms(maximum = Infinity) {
                const remaining = Math.floor(Math.min(maximum, start + 7000 - performance.now()));
                if (remaining < 75) throw new JobError('time_budget_exhausted');
                return remaining;
            } };
            const [snapshot] = await Promise.all([api.snapshot(slot, budget), ledger.get(slot, budget)]);
            assertActivity(plan, snapshot.activity);
            console.log(JSON.stringify({ status: 'probe_ok', database: 'reachable', receipt: recordStatus(snapshot.rows),
                duration_ms: Math.round(performance.now() - start) }));
            return;
        }
        const status = await runJob({ plan, api, ledger, photoBytes: photo.length });
        if (!['submitted', 'existing_submitted', 'no_matching_window'].includes(status)) process.exitCode = 1;
    } finally {
        agent.destroy();
    }
}

if (require.main === module) {
    main().catch((error) => {
        console.log(JSON.stringify({ status: statusOf(error) }));
        process.exitCode = 1;
    });
}

module.exports = { runJob, selectSlot };
