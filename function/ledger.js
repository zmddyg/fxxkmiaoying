'use strict';

const { JobError } = require('./miaoying');

const COLLECTION = 'miaoying_class_attendance_v1';
let collection;

async function databaseCall(work) {
    try {
        const result = await work();
        if (result && !result.code) return result;
    } catch (error) {
        if (error instanceof JobError) throw error;
    }
    throw new JobError('cloud_database_unavailable');
}

function getCollection() {
    if (!collection) {
        const sdk = require('./cloudbase-sdk');
        const { TCB_ENV_ID, TENCENT_SECRET_ID, TENCENT_SECRET_KEY, GITHUB_ACTIONS } = process.env;
        const external = GITHUB_ACTIONS === 'true' || Boolean(TENCENT_SECRET_ID || TENCENT_SECRET_KEY);
        if (external && (!TCB_ENV_ID || !TENCENT_SECRET_ID || !TENCENT_SECRET_KEY)) {
            throw new JobError('cloud_database_configuration_missing');
        }
        const credentials = external
            ? { env: TCB_ENV_ID, secretId: TENCENT_SECRET_ID, secretKey: TENCENT_SECRET_KEY }
            : { env: sdk.SYMBOL_CURRENT_ENV };
        collection = sdk.init({ ...credentials, keepalive: true, timeout: 1300, retries: 0 })
            .database().collection(COLLECTION);
    }
    return collection;
}

function createLedger() {
    return {
        async get(slot, budget) {
            const query = getCollection().where({ _id: slot.key }).limit(1);
            const result = await databaseCall(() => query.options({ timeout: budget.ms(1300) }).get());
            if (!Array.isArray(result.data) || result.data.length > 1) throw new JobError('cloud_database_invalid_response');
            return result.data[0] || null;
        },
        async add(slot, contents, budget) {
            // CloudBase adds a uniquely keyed document. Never use set/upsert here.
            const result = await databaseCall(() => getCollection().options({ timeout: budget.ms(1300) })
                .add({ _id: slot.key, photo: slot.photo, day: slot.day, slot: slot.slot, ...contents }));
            if (result.id !== slot.key) throw new JobError('cloud_database_add_unconfirmed');
        },
        async move(slot, conditions, contents, budget) {
            const result = await databaseCall(() => getCollection().where({ _id: slot.key, ...conditions })
                .options({ timeout: budget.ms(1300) }).update(contents));
            if (result.updated !== 0 && result.updated !== 1) throw new JobError('cloud_database_update_unconfirmed');
            return result.updated === 1;
        },
    };
}

module.exports = { createLedger };
