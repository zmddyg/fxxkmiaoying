'use strict';

const https = require('node:https');
const { randomUUID } = require('node:crypto');

const GRAPHQL = 'https://www.aiphoto8.cn/api/graphql';
const SIGNER = 'https://miaoying.hui51.cn/api/file/getOssSign';
const STORAGE = 'https://hui51.oss-cn-beijing.aliyuncs.com/';
const SIGNING_FIELDS = ['policy', 'signature', 'OSSAccessKeyId'];
const RECORDS = 'baomingsWithCount(tongjiId: $activity, userId: $user, submitDayStr: $day, submitDayIndex: $slot, limit: "2") { total baomings { _id tongjiId createdBy isRemove isPassSecurity submitDayStr submitDayIndex createdAt formVal { id value } locationInfo { longtitude lattitude } } }';
const VARIABLES = '($activity: String, $user: String, $day: String, $slot: Int)';
const REGISTER = 'mutation createMediaFileByInput($input: createMediaFileInput!) { createMediaFileByInput(input: $input) { _id } }';
const SUBMIT = 'mutation createBaomingByInput($input: createBaomingInput!) { createBaomingByInput(input: $input) { _id isPassSecurity } }';

class JobError extends Error {
    constructor(status) {
        super(status);
        this.status = status;
    }
}

function post(url, data, headers, limit, operation, agent, abort) {
    return new Promise((resolve, reject) => {
        const unavailable = () => reject(new JobError(operation + '_unavailable'));
        const timeout = AbortSignal.timeout(limit);
        const signal = abort ? AbortSignal.any([timeout, abort]) : timeout;
        const request = https.request(url, {
            method: 'POST', agent, signal,
            headers: { ...headers, 'Content-Length': data.length },
        }, (response) => {
            const parts = [];
            let size = 0;
            response.on('data', (chunk) => {
                size += chunk.length;
                if (size > 131072) {
                    reject(new JobError(operation + '_response_too_large'));
                    response.destroy();
                    return;
                }
                parts.push(chunk);
            });
            response.on('error', unavailable);
            response.on('aborted', unavailable);
            response.on('end', () => resolve({
                status: response.statusCode, text: Buffer.concat(parts).toString('utf8'),
            }));
        });
        request.on('error', unavailable);
        request.end(data);
    });
}

function parse(response, operation) {
    if (response.status === 401) throw new JobError('reauthorization_required');
    if (response.status !== 200) throw new JobError(operation + '_rejected');
    try {
        const doc = JSON.parse(response.text);
        if (doc && typeof doc === 'object' && !Array.isArray(doc)) return doc;
    } catch (_) {
        // The response text can contain account data. Do not log it.
    }
    throw new JobError(operation + '_invalid_response');
}

function authError(error) {
    return ['UNAUTHENTICATED', 'INVALID_TOKEN', 'TOKEN_EXPIRED'].includes(error?.extensions?.code)
        || /未登录|请(先|重新)登录|登录.{0,8}(过期|失效)|jwt expired|invalid token|unauthenticated/i.test(error?.message || '');
}

function multipart(boundary, textFields, photo) {
    const parts = Object.entries(textFields).map(([name, value]) => Buffer.from(
        '--' + boundary + '\r\nContent-Disposition: form-data; name="' + name + '"\r\n\r\n' + value + '\r\n',
    ));
    if (photo) {
        parts.push(Buffer.from('--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="'
            + photo.name + '"\r\nContent-Type: image/jpeg\r\n\r\n'), photo.bytes, Buffer.from('\r\n'));
    }
    parts.push(Buffer.from('--' + boundary + '--\r\n'));
    return Buffer.concat(parts);
}

function validSigning(signing, bytes, at = Date.now()) {
    return signing && Number.isFinite(signing.expires) && signing.expires > at + 5000
        && Number.isFinite(signing.maxSize) && bytes <= signing.maxSize * 1048576
        && SIGNING_FIELDS.every((field) => typeof signing[field] === 'string'
            && signing[field].length > 0 && signing[field].length < 16384 && !/[\r\n]/.test(signing[field]));
}

function validateRecords(snapshot, plan, slot) {
    const ledger = snapshot.baomingsWithCount;
    const rows = ledger?.baomings;
    if (!Number.isInteger(ledger?.total) || ledger.total < 0 || !Array.isArray(rows)
        || rows.length !== Math.min(2, ledger.total) || rows.some((r) => !r || typeof r._id !== 'string' || !r._id
            || r.tongjiId !== plan.payload.tongjiId || r.createdBy !== plan.payload.userId
            || r.submitDayStr !== slot.day || r.submitDayIndex !== slot.slot)) {
        throw new JobError('records_invalid_response');
    }
    return rows;
}

function makeAgent() {
    // Node 24 can honor the local HTTPS proxy. Earlier runtimes ignore proxyEnv.
    return new https.Agent({ keepAlive: true, maxSockets: 4, maxFreeSockets: 2,
        timeout: 12000, proxyEnv: process.env });
}

function createApi(plan, token, agent, photo) {
    if (typeof token !== 'string' || !token.trim() || /[\r\n]/.test(token)) throw new JobError('session_token_missing');
    const authorization = { Authorization: 'Bearer ' + token, Accept: 'application/json' };
    const snapshotQuery = 'query AttendanceSnapshot' + VARIABLES + ' { me { _id } ' + plan.activitySelection + ' ' + RECORDS + ' }';
    const recordsQuery = 'query AttendanceReceipt' + VARIABLES + ' { me { _id } ' + RECORDS + ' }';

    async function graphql(operation, query, variables, budget, abort) {
        const response = await post(GRAPHQL, Buffer.from(JSON.stringify({ operationName: operation, query, variables })),
            { ...authorization, 'Content-Type': 'application/json' }, budget.ms(), operation, agent, abort);
        const doc = parse(response, operation);
        if (Array.isArray(doc.errors) && doc.errors.length) {
            if (doc.errors.every(authError)) throw new JobError('reauthorization_required');
            throw new JobError(operation + '_rejected');
        }
        if (!doc.data || typeof doc.data !== 'object') throw new JobError(operation + '_invalid_response');
        return doc.data;
    }

    async function read(slot, withActivity, budget, abort) {
        const variables = { activity: plan.payload.tongjiId, user: plan.payload.userId, day: slot.day, slot: slot.slot };
        const data = withActivity ? await graphql('AttendanceSnapshot', snapshotQuery, variables, budget, abort)
            : await graphql('AttendanceReceipt', recordsQuery, variables, budget);
        if (!data.me?._id) throw new JobError('reauthorization_required');
        if (data.me._id !== variables.user) throw new JobError('account_mismatch');
        return { activity: data.tongji, rows: validateRecords(data, plan, slot) };
    }

    return {
        snapshot: (slot, budget, abort) => read(slot, true, budget, abort),
        records: async (slot, budget) => (await read(slot, false, budget)).rows,
        async sign(budget) {
            const boundary = randomUUID();
            const response = await post(SIGNER, multipart(boundary, {}),
                { ...authorization, 'Content-Type': 'multipart/form-data; boundary=' + boundary }, budget.ms(), 'signer', agent);
            const doc = parse(response, 'signer');
            if (doc.statusCode === 401) throw new JobError('reauthorization_required');
            const data = doc.data;
            if (doc.statusCode !== 200 || !data || ![STORAGE, STORAGE.slice(0, -1)].includes(data.uploadImageUrl)) {
                throw new JobError('upload_destination_unverified');
            }
            const signing = Object.fromEntries(SIGNING_FIELDS.map((field) => [field, data[field]]));
            try {
                signing.expires = Date.parse(JSON.parse(Buffer.from(signing.policy, 'base64').toString('utf8')).expiration);
            } catch (_) {
                throw new JobError('upload_policy_invalid');
            }
            signing.maxSize = data.maxSize;
            if (!validSigning(signing, photo.length)) throw new JobError('upload_policy_invalid');
            return signing;
        },
        async upload(slot, signing, budget) {
            if (!validSigning(signing, photo.length)) throw new JobError('upload_policy_expired');
            const boundary = randomUUID();
            const fields = Object.fromEntries(SIGNING_FIELDS.map((field) => [field, signing[field]]));
            Object.assign(fields, { key: 'uploads/' + slot.photo, success_action_status: '200' });
            const response = await post(STORAGE, multipart(boundary, fields, { name: slot.photo, bytes: photo }),
                { 'Content-Type': 'multipart/form-data; boundary=' + boundary }, budget.ms(), 'storage', agent);
            if (response.status !== 200) throw new JobError('storage_rejected');
        },
        async register(slot, budget) {
            const result = await graphql('createMediaFileByInput', REGISTER, { input: {
                ossPath: 'uploads/' + slot.photo, bucket: 'hui51', fileSize: photo.length,
                entity: 'tongji', entityId: plan.payload.tongjiId,
            } }, budget);
            const id = result.createMediaFileByInput?._id;
            if (typeof id !== 'string' || !id) throw new JobError('media_registration_unconfirmed');
        },
        async submit(slot, budget) {
            const result = await graphql('createBaomingByInput', SUBMIT, { input: slot.input }, budget);
            return typeof result.createBaomingByInput?._id === 'string' && Boolean(result.createBaomingByInput._id);
        },
    };
}

module.exports = { JobError, createApi, makeAgent, validSigning };
