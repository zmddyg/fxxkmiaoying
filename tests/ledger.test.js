'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const sdkPath = require.resolve('../function/cloudbase-sdk');
const ledgerPath = require.resolve('../function/ledger');
const environmentKeys = ['TCB_ENV_ID', 'TENCENT_SECRET_ID', 'TENCENT_SECRET_KEY', 'GITHUB_ACTIONS'];
const currentEnv = Symbol('current environment');
const budget = { ms: (maximum) => maximum };

async function withEnvironment(values, action) {
    const originalEnv = Object.fromEntries(environmentKeys.map((key) => [key, process.env[key]]));
    const originalSdk = require.cache[sdkPath];
    const originalLedger = require.cache[ledgerPath];
    let configuration;
    const collection = {
        where() { return this; },
        limit() { return this; },
        options() { return this; },
        async get() { return { data: [] }; },
    };
    require.cache[sdkPath] = { id: sdkPath, filename: sdkPath, loaded: true, exports: {
        SYMBOL_CURRENT_ENV: currentEnv,
        init(config) {
            configuration = config;
            return { database: () => ({ collection: () => collection }) };
        },
    } };
    delete require.cache[ledgerPath];
    for (const key of environmentKeys) {
        if (values[key] === undefined) delete process.env[key];
        else process.env[key] = values[key];
    }
    try {
        const ledger = require('../function/ledger').createLedger();
        await action(ledger, () => configuration);
    } finally {
        for (const key of environmentKeys) {
            if (originalEnv[key] === undefined) delete process.env[key];
            else process.env[key] = originalEnv[key];
        }
        if (originalSdk) require.cache[sdkPath] = originalSdk;
        else delete require.cache[sdkPath];
        if (originalLedger) require.cache[ledgerPath] = originalLedger;
        else delete require.cache[ledgerPath];
    }
}

test('SCF uses its current environment and platform credentials', async () => {
    await withEnvironment({ TCB_ENV_ID: 'platform-provided' }, async (ledger, configuration) => {
        assert.equal(await ledger.get({ key: 'test' }, budget), null);
        assert.equal(configuration().env, currentEnv);
        assert.equal(configuration().secretId, undefined);
    });
});

test('GitHub Actions supplies explicit CloudBase environment and credentials', async () => {
    await withEnvironment({ GITHUB_ACTIONS: 'true', TCB_ENV_ID: 'example-env',
        TENCENT_SECRET_ID: 'example-id', TENCENT_SECRET_KEY: 'example-key' }, async (ledger, configuration) => {
        assert.equal(await ledger.get({ key: 'test' }, budget), null);
        assert.equal(configuration().env, 'example-env');
        assert.equal(configuration().secretId, 'example-id');
        assert.equal(configuration().secretKey, 'example-key');
    });
});

test('GitHub Actions fails closed when credentials are incomplete', async () => {
    await withEnvironment({ GITHUB_ACTIONS: 'true', TCB_ENV_ID: 'example-env', TENCENT_SECRET_ID: 'example-id' },
        async (ledger, configuration) => {
            await assert.rejects(ledger.get({ key: 'test' }, budget), { status: 'cloud_database_configuration_missing' });
            assert.equal(configuration(), undefined);
        });
});
