-- Migration: instant 86 for menu items + shift call-out status
-- Product.is_86ed / Product.quantity_available  (menu manager 86 toggles)
-- Shift.status / Shift.callout_reason          (emergency call-out workflow)

DO $$ BEGIN
    CREATE TYPE shift_status AS ENUM ('active', 'called_out', 'covered');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

ALTER TABLE shift
  ADD COLUMN IF NOT EXISTS status shift_status NOT NULL DEFAULT 'active',
  ADD COLUMN IF NOT EXISTS callout_reason VARCHAR(255);

ALTER TABLE product
  ADD COLUMN IF NOT EXISTS is_86ed BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS quantity_available INTEGER;

CREATE INDEX IF NOT EXISTS idx_shift_status ON shift (status);
CREATE INDEX IF NOT EXISTS idx_product_is_86ed ON product (is_86ed);
