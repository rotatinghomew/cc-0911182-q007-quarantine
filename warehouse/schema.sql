CREATE TABLE IF NOT EXISTS receipts(batch_id VARCHAR PRIMARY KEY, signature VARCHAR NOT NULL, inserted INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS source_rows(table_name VARCHAR, row_key VARCHAR, payload VARCHAR NOT NULL, batch_id VARCHAR NOT NULL, PRIMARY KEY(table_name,row_key));
CREATE TABLE IF NOT EXISTS programs(partner VARCHAR, program_id VARCHAR, title VARCHAR, active BOOLEAN, PRIMARY KEY(partner,program_id));
CREATE TABLE IF NOT EXISTS editions(partner VARCHAR, edition_id VARCHAR, program_id VARCHAR, language VARCHAR, duration INTEGER, PRIMARY KEY(partner,edition_id));
CREATE TABLE IF NOT EXISTS grants(partner VARCHAR, grant_id VARCHAR, edition_id VARCHAR, region VARCHAR, valid_from VARCHAR, valid_to VARCHAR, PRIMARY KEY(partner,grant_id));
CREATE TABLE IF NOT EXISTS supply_packages(package_id VARCHAR PRIMARY KEY, partner VARCHAR, body VARCHAR, state VARCHAR);

CREATE TABLE IF NOT EXISTS distribution_releases(release_id VARCHAR PRIMARY KEY, business_day VARCHAR, rows_json VARCHAR);

-- 逐行判定账本：历史包的原始内容与每次判定依据只追加，不覆盖
CREATE TABLE IF NOT EXISTS row_events(
    seq INTEGER PRIMARY KEY,
    batch_id VARCHAR NOT NULL,
    table_name VARCHAR NOT NULL,
    row_key VARCHAR NOT NULL,
    action VARCHAR NOT NULL,
    verdict VARCHAR NOT NULL,
    reason VARCHAR,
    payload VARCHAR NOT NULL
);
CREATE SEQUENCE IF NOT EXISTS row_events_seq START 1;

-- 尚未进入可分发集市的记录：invalid=自身字段错误，waiting=依赖未满足
CREATE TABLE IF NOT EXISTS pending_rows(
    table_name VARCHAR NOT NULL,
    row_key VARCHAR NOT NULL,
    partner VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    batch_id VARCHAR NOT NULL,
    state VARCHAR NOT NULL,
    reason VARCHAR NOT NULL,
    payload VARCHAR NOT NULL,
    PRIMARY KEY(table_name,row_key)
);

-- 正式撤销的业务标识：撤销后自身及其依赖停止可用，历史事实保留
CREATE TABLE IF NOT EXISTS revocations(
    table_name VARCHAR NOT NULL,
    row_key VARCHAR NOT NULL,
    partner VARCHAR NOT NULL,
    record_id VARCHAR NOT NULL,
    batch_id VARCHAR NOT NULL,
    reason VARCHAR,
    PRIMARY KEY(table_name,row_key)
);

-- 每个批次（供稿包/修正）的逐项判定计数，供运营核对原包总数
CREATE TABLE IF NOT EXISTS batch_reports(
    batch_id VARCHAR PRIMARY KEY,
    total_rows INTEGER NOT NULL,
    accepted INTEGER NOT NULL,
    invalid INTEGER NOT NULL,
    waiting INTEGER NOT NULL,
    revoked INTEGER NOT NULL,
    blocked INTEGER NOT NULL,
    summary_json VARCHAR NOT NULL
);
