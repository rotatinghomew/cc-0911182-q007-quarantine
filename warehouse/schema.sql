CREATE TABLE IF NOT EXISTS receipts(batch_id VARCHAR PRIMARY KEY, signature VARCHAR NOT NULL, inserted INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS source_rows(table_name VARCHAR, row_key VARCHAR, payload VARCHAR NOT NULL, batch_id VARCHAR NOT NULL, PRIMARY KEY(table_name,row_key));
        CREATE TABLE IF NOT EXISTS programs(partner VARCHAR, program_id VARCHAR, title VARCHAR, active BOOLEAN, PRIMARY KEY(partner,program_id));
        CREATE TABLE IF NOT EXISTS editions(partner VARCHAR, edition_id VARCHAR, program_id VARCHAR, language VARCHAR, duration INTEGER, PRIMARY KEY(partner,edition_id));
        CREATE TABLE IF NOT EXISTS grants(partner VARCHAR, grant_id VARCHAR, edition_id VARCHAR, region VARCHAR, valid_from VARCHAR, valid_to VARCHAR, PRIMARY KEY(partner,grant_id));
        CREATE TABLE IF NOT EXISTS supply_packages(package_id VARCHAR PRIMARY KEY, partner VARCHAR, body VARCHAR, state VARCHAR);

CREATE TABLE IF NOT EXISTS distribution_releases(release_id VARCHAR PRIMARY KEY, business_day VARCHAR, rows_json VARCHAR);