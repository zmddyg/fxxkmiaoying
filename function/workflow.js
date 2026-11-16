'use strict';

const { randomUUID } = require('node:crypto');
const { isDeepStrictEqual } = require('node:util');
const { JobError, validSigning } = require('./miaoying');

const MINUTE = 60000;
const matchPhoto = /^[A-Za-z0-9][A-Za-z0-9_-]{0,95}\.jpg$/;

function statusOf(error) {
    return error instanceof JobError ? error.status : 'internal_error';
}

function buildSchedule(plan) {
    const times = new Map();
    const occurrences = new Map();
    for (const slot of plan.occurrences) {
        if (!matchPhoto.test(slot.photo) || slot.opens % MINUTE !== 0 || slot.closes <= slot.opens) {
            throw new Error('invalid occurrence in plan');
        }
        const formVal = plan.payload.formVal.map((field, index) => index === plan.photoIndex
            ? { ...field, value: slot.photo } : field);
        const infoVal = plan.payload.infoVal.map((value, index) => index === plan.photoInfoIndex ? slot.photo : value);
        const input = { ...plan.payload, formVal, infoVal, submitDayStr: slot.day, submitDayIndex: slot.slot };
        if (slot.location) input.locationInfo = slot.location;
        const prepared = { ...slot, input };
        occurrences.set(slot.day + '/' + slot.slot, prepared);
        for (const offset of plan.offsetMinutes) {
            const minute = slot.opens / MINUTE + offset;
            if (times.has(minute)) throw new Error('overlapping scheduled triggers');
            times.set(minute, { slot: prepared, offset });
        }
    }
    return { times, occurrences };
}

function assertActivity(plan, config) {
    if (!config) throw new JobError('activity_unavailable');
    const { isClosed, isRemove, publishResult, ...rules } = config;
    if (isRemove !== false || (isClosed !== false && isClosed !== null)) throw new JobError('activity_closed');
    if (!isDeepStrictEqual(rules, plan.activityRules)) throw new JobError('activity_rules_changed');
}

function recordStatus(rows) {
    if (rows.length === 0) return 'no_record';
    if (rows.length > 1) return 'multiple_records';
    if (rows[0].isRemove === true) return 'previous_record_removed';
    return rows[0].isPassSecurity === true ? 'submitted' : 'record_persisted_review_unconfirmed';
}

function hasMatchingEvidence(slot, state, row) {
    if (!Array.isArray(row.formVal) || !row.formVal.some((value) => value?.id === state.photoField
        && typeof value.value === 'string' && value.value.split(',').includes(state.photo))) return false;
    if (!slot.location) return true;
    const { longtitude, lattitude } = row.locationInfo || {};
    return Number.isFinite(longtitude) && Number.isFinite(lattitude)
        && Math.abs(longtitude - slot.location.longtitude) < 0.00000005
        && Math.abs(lattitude - slot.location.lattitude) < 0.00000005;
}

async function prepare(slot, state, job) {
    const { plan, api, ledger, budget, photoBytes, now } = job;
    if (state && state.photo !== slot.photo) return 'prepared_photo_changed';
    switch (state?.phase) {
        case undefined: {
            const [snapshot, signing] = await Promise.all([api.snapshot(slot, budget), api.sign(budget)]);
            assertActivity(plan, snapshot.activity);
            if (snapshot.rows.length) return 'existing_' + recordStatus(snapshot.rows);
            await ledger.add(slot, { phase: 'signed', signing }, budget);
            return 'photo_signed';
        }
        case 'signed': {
            if (!validSigning(state.signing, photoBytes, now())) {
                // Only reset an unused policy, never overwrite a subsequent phase.
                const signing = await api.sign(budget);
                const changed = await ledger.move(slot, { phase: 'signed', photo: slot.photo }, { signing }, budget);
                return changed ? 'photo_signed' : 'phase_already_advanced';
            }
            // Retrying an uncertain upload overwrites the same object with the same bytes.
            await api.upload(slot, state.signing, budget);
            const changed = await ledger.move(slot, { phase: 'signed', photo: slot.photo },
                { phase: 'uploaded', signing: null }, budget);
            return changed ? 'photo_uploaded' : 'phase_already_advanced';
        }
        case 'uploaded':
        case 'registering': {
            // The lease prevents concurrent media registrations for duplicate timer events.
            // If media registration loses its response, a later invocation can retry it.
            if (state.phase === 'registering' && state.registerLeaseUntil > now()) return 'photo_registration_in_progress';
            const conditions = { phase: state.phase, photo: slot.photo };
            if (state.phase === 'registering') conditions.registerLeaseUntil = state.registerLeaseUntil;
            const lease = now() + 10000;
            const reserved = await ledger.move(slot, conditions, { phase: 'registering', registerLeaseUntil: lease }, budget);
            if (!reserved) return 'phase_already_advanced';
            await api.register(slot, budget);
            const changed = await ledger.move(slot, { phase: 'registering', registerLeaseUntil: lease }, { phase: 'ready' }, budget);
            return changed ? 'photo_ready' : 'photo_registration_unconfirmed';
        }
        default:
            return state.phase === 'ready' ? 'photo_ready' : 'already_processed';
    }
}

async function dispatch(slot, job) {
    const { plan, api, ledger, budget, now, timings } = job;
    if (now() < slot.opens || now() >= slot.closes) return 'outside_class_window';
    if (budget.ms() < 1000) return 'insufficient_time_for_preflight';
    const started = performance.now();
    const claim = randomUUID();
    const photoField = plan.payload.formVal[plan.photoIndex].id;
    const guard = { phase: 'dispatching', claim };
    const preflightBudget = {
        ms(maximum = Infinity) {
            const ms = Math.min(budget.ms() - 650, 1450, maximum);
            if (ms < 75) throw new JobError('insufficient_time_for_preflight');
            return ms;
        },
    };
    // Claiming and reading the latest rules overlap. If the claim fails, cancel the now-useless read.
    const controller = new AbortController();
    const snapshot = api.snapshot(slot, preflightBudget, controller.signal).then(
        (value) => ({ value }), (error) => ({ error }),
    );
    let blocked;
    try {
        const claimed = await ledger.move(slot, { phase: 'ready', photo: slot.photo },
            { ...guard, photoField, claimAt: now() }, preflightBudget);
        if (!claimed) blocked = 'photo_not_ready_or_already_claimed';
    } catch (error) {
        blocked = statusOf(error);
    }
    if (blocked) {
        controller.abort();
        timings.preflight_ms = Math.round(performance.now() - started);
        return blocked;
    }
    const snapshotResult = await snapshot;
    timings.preflight_ms = Math.round(performance.now() - started);

    let skip;
    let existing;
    try {
        if (snapshotResult.error) throw snapshotResult.error;
        assertActivity(plan, snapshotResult.value.activity);
        existing = snapshotResult.value.rows;
        if (existing.length) skip = 'existing_' + recordStatus(existing);
        else if (now() < slot.opens || now() >= slot.closes) skip = 'outside_class_window';
        else if (budget.ms() < 550) skip = 'insufficient_time_to_submit';
    } catch (error) {
        skip = statusOf(error);
    }
    if (skip) {
        // This branch has not made a submit call. Only the owning invocation may release its claim.
        try {
            if (existing?.length) {
                await ledger.move(slot, guard, { phase: 'existing', result: skip }, budget);
            } else {
                await ledger.move(slot, guard, { phase: 'ready', claim: null }, budget);
            }
        } catch (_) {
            return skip + '_claim_retained';
        }
        return skip;
    }

    const sending = performance.now();
    try {
        const acknowledged = await api.submit(slot, budget);
        return acknowledged ? 'submission_acknowledged_waiting_for_receipt' : 'submission_outcome_unknown';
    } catch (error) {
        // A transport error or non-success response can occur after the server has written a record.
        return 'submission_outcome_unknown';
    } finally {
        timings.submit_ms = Math.round(performance.now() - sending);
    }
}

async function reconcile(slot, state, job) {
    const { api, ledger, budget } = job;
    const rows = await api.records(slot, budget);
    const status = recordStatus(rows);
    if (status === 'no_record') return 'submission_outcome_unknown';
    if (status === 'multiple_records' || status === 'previous_record_removed') return status;
    if (!hasMatchingEvidence(slot, state, rows[0])) return 'receipt_does_not_match';
    if (state.phase === 'verified' && status === 'submitted') return status;
    await ledger.move(slot, { phase: state.phase, claim: state.claim }, {
        phase: status === 'submitted' ? 'verified' : 'dispatching', result: status,
        receiptId: rows[0]._id, receivedAt: rows[0].createdAt ?? null,
    }, budget);
    return status;
}

async function runStage(slot, offset, job) {
    const { ledger, budget, now } = job;
    if (offset === 0 && now() < slot.closes) return dispatch(slot, job);
    const state = await ledger.get(slot, budget);
    const afterWindow = now() >= slot.closes;
    if (state?.phase === 'dispatching' || (afterWindow && state?.phase === 'verified')) {
        return reconcile(slot, state, job);
    }
    if (state?.phase === 'verified') return 'submitted';
    if (state?.phase === 'existing' && !afterWindow) return state.result;
    if (afterWindow) {
        if (state?.signing) await ledger.move(slot, { phase: 'signed' }, { phase: 'expired', signing: null }, budget);
        const rows = await job.api.records(slot, budget);
        return rows.length ? 'existing_' + recordStatus(rows) : 'window_closed_no_submission';
    }
    if (offset > 0 && state?.phase === 'ready') return dispatch(slot, job);
    return prepare(slot, state, job);
}

module.exports = { MINUTE, assertActivity, buildSchedule, recordStatus, runStage, statusOf };
