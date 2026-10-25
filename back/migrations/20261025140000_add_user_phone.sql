-- Migration: staff phone number for emergency shift call-out SMS broadcasts.
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS phone VARCHAR(64);
