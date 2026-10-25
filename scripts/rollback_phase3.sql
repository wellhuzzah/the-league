-- Reverses the audit Phase 3 schema changes (schema_v3.sql section 5). Run as postgres.
-- rollback_phase3.ps1 runs this, then restores the two dropped tables (draft_order and
-- box_scores_backup_20261007, with their data, sequence, keys and grants) from
-- D:\Backup\rockwood\db\rockwood_pre_phase3.dump with a filtered pg_restore.
\set ON_ERROR_STOP on
BEGIN;

DROP VIEW IF EXISTS team_games;

-- is_starter back to a plain column (values kept), plus the CHECK it used to have. The column
-- stays last in the table instead of after lineup_slot; every INSERT names its columns.
ALTER TABLE box_scores ALTER COLUMN is_starter DROP EXPRESSION;
ALTER TABLE box_scores ADD CONSTRAINT box_scores_starter_from_slot
	CHECK (is_starter = (lineup_slot <> ALL (ARRAY[20, 21])));

-- Was NULL in all 248 rows, read and written by nothing.
ALTER TABLE espn_team_map ADD COLUMN IF NOT EXISTS team_name character varying(100);

-- Added after Phase 3 (schema_v3.sql section 6); the pre-Phase-3 importer doesn't know it.
ALTER TABLE box_scores DROP COLUMN IF EXISTS played;

COMMIT;
