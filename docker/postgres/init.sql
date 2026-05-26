-- Simple schema for local development
-- Only includes what's needed for API key validation

-- Create the keys database
CREATE DATABASE keys;

-- Connect to the keys database
\c keys

-- Tiers table
CREATE TABLE tiers (
    tier_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(50) UNIQUE NOT NULL
);

-- Keys table (simplified, no user foreign key)
CREATE TABLE keys (
    key_id UUID PRIMARY KEY,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Key-tier assignments table
CREATE TABLE key_tier_assignments (
    key_id UUID NOT NULL,
    tier_id UUID NOT NULL,
    PRIMARY KEY (key_id, tier_id),
    FOREIGN KEY (key_id) REFERENCES keys(key_id) ON DELETE CASCADE,
    FOREIGN KEY (tier_id) REFERENCES tiers(tier_id) ON DELETE RESTRICT
);

-- Index for key lookups
CREATE INDEX idx_keys_key_id ON keys(key_id);

-- Insert hotswapping tier
INSERT INTO tiers (name)
VALUES ('hotswapping');

-- Insert test API key (this UUID will be used in scripts/test.py)
INSERT INTO keys (key_id)
VALUES ('12345678-1234-5678-1234-567812345678');

-- Grant hotswapping access to test key
INSERT INTO key_tier_assignments (key_id, tier_id)
SELECT '12345678-1234-5678-1234-567812345678', tier_id
FROM tiers
WHERE name = 'hotswapping';
