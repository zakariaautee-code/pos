-- Repair databases where SQLModel.create_all created `floor` before the
-- migrations ran: the columns already existed, so ADD COLUMN ... DEFAULT ...
-- IF NOT EXISTS was skipped and no server default remains. Raw-SQL seed
-- inserts (app.seeds.seed_demo_tables) rely on these defaults.
ALTER TABLE floor ALTER COLUMN is_active SET DEFAULT TRUE;
ALTER TABLE floor ALTER COLUMN seating_zone SET DEFAULT 'any';
