'use strict';

const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const plan = require('./plan.json');
const { JobError, createApi, makeAgent } = require('./miaoying');
const { createLedger } = require('./ledger');
const { MINUTE, assertActivity, buildSchedule, recordStatus, runStage, statusOf } = require('./workflow');

const { times, occurrences } = buildSchedule(plan);
const photo = readFileSync(join(__dirname, 'black.jpg'));
let agent;

function taskAt(value) {
    if (typeof value !== 'string' || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return null;
    const at = Date.parse(value);
    if (!Number.isFinite(at)) return null;
    const task = times.get(Math.floor(at / MINUTE));
    return task ? { ...task, at } : null;
}

function makeBudget(start) {
    const limit = start + 2700;
    return {
        ms(maximum = Infinity) {
            const ms = Math.floor(Math.min(maximum, limit - performance.now()));
            if (ms < 75) throw new JobError('time_budget_exhausted');
            return ms;
        },
    };
}

function describe(task, status) {
    return { status, day: task.slot.day, slot: task.slot.slot, offset_minutes: task.offset };
}

function apiForSession() {
    agent ??= makeAgent();
    return createApi(plan, process.env.MIAOYING_SESSION_TOKEN, agent, photo);
}

function timedCalls(target, bucket) {
    return Object.fromEntries(Object.entries(target).map(([key, action]) => [key, async (...args) => {
        const started = performance.now();
        try {
            return await action(...args);
        } finally {
            (bucket[key] ??= []).push(Math.round(performance.now() - started));
        }
    }]));
}

async function probe(event, budget, started) {
    const slot = occurrences.get(event.day + '/' + event.slot);
    if (!slot) return { status: 'probe_slot_not_found' };
    const api = apiForSession();
    const read = api.snapshot(slot, budget).then(({ activity, rows }) => {
        assertActivity(plan, activity);
        return { receipt: recordStatus(rows), snapshot_ms: Math.round(performance.now() - started) };
    });
    const db = event.database === false ? Promise.resolve('skipped')
        : createLedger().get(slot, budget).then(() => 'reachable');
    const [readResult, databaseResult] = await Promise.allSettled([read, db]);
    if (readResult.status === 'rejected') return { status: statusOf(readResult.reason) };
    const database = databaseResult.status === 'fulfilled' ? databaseResult.value : statusOf(databaseResult.reason);
    return { status: databaseResult.status === 'fulfilled' ? 'probe_ok' : 'probe_database_failed',
        database, ...readResult.value, total_ms: Math.round(performance.now() - started) };
}

exports.main = async (event = {}) => {
    const started = performance.now();
    const budget = makeBudget(started);
    try {
        if (event.mode === 'probe') return await probe(event, budget, started);
        if (event.mode === 'preview') {
            const task = taskAt(event.at);
            return task ? describe(task, 'matched_schedule') : { status: 'no_scheduled_class' };
        }
        if (event.Type !== 'Timer') return { status: 'expected_timer_event' };
        if (plan.requireCloudTimer === true && process.env.TENCENTCLOUD_RUNENV === 'SCF'
            && process.env.TRIGGER_SRC !== 'timer') {
            return { status: 'expected_platform_timer' };
        }
        const task = taskAt(event.Time);
        if (!task) return { status: 'no_scheduled_class',
            ...(event.TriggerName === 'class_clock_check' && typeof event.Time === 'string'
                ? { scheduled_at: event.Time, trigger: event.TriggerName } : {}) };
        const now = Date.now();
        if (now < task.at - 3000 || now > task.at + 240000) return describe(task, 'timer_outside_expected_time');
        if (process.env.MIAOYING_ENABLE_SUBMIT !== '1') {
            const result = describe(task, 'timer_observed');
            console.log(JSON.stringify(result));
            return result;
        }

        const timings = {};
        const job = { plan, api: apiForSession(), ledger: createLedger(), budget, now: Date.now,
            timings, photoBytes: photo.length };
        if (plan.traceStages === true) {
            console.log(JSON.stringify(describe(task, 'timer_started')));
            job.api = timedCalls(job.api, timings.miaoying_ms = {});
            job.ledger = timedCalls(job.ledger, timings.database_ms = {});
        }
        let status;
        try {
            status = await runStage(task.slot, task.offset, job);
        } catch (error) {
            status = statusOf(error);
        }
        const result = { ...describe(task, status), ...timings, total_ms: Math.round(performance.now() - started) };
        console.log(JSON.stringify(result));
        return result;
    } catch (error) {
        const result = { status: statusOf(error), total_ms: Math.round(performance.now() - started) };
        console.log(JSON.stringify(result));
        return result;
    }
};
