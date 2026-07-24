-- Dev/local bootstrap for the NDIF user/keys database.
--
-- Mirrors the production schema (users / profiles / user_tags / keys / ...) so
-- the API's verify_api_key exercises the real query, and creates the read-only
-- `ndifapi` role the API connects as plus the `login_page` role the account
-- portal uses.
--
-- No users or API keys are seeded — the stack runs unauthenticated by default
-- (docker-compose.yml leaves NDIF_POSTGRES_URL unset). If you enable auth, add
-- users/keys via the login page (or manually as `login_page`).
--
-- Runs once, as the superuser, inside the POSTGRES_DB database
-- (docker-compose.yml sets POSTGRES_DB=ndif) — no CREATE DATABASE needed.

---
--- ENUMS
---

CREATE TYPE verification_status_enum AS ENUM (
    'unverified',
    'pending',
    'verified'
);

CREATE TYPE attestation_type_enum AS ENUM (
    'us-based',
    'us-collaborator',
    'no-collaborator'
);

CREATE TYPE audit_action_enum AS ENUM (
    'user_created',
    'user_updated',
    'user_deleted',
    'profile_created',
    'profile_updated',
    'profile_deleted',
    'key_created',
    'key_updated',
    'key_deleted',
    'key_user_tag_assigned',
    'key_user_tag_unassigned',
    'model_created',
    'model_updated',
    'model_deleted',
    'model_tag_assigned',
    'model_tag_unassigned'
);

---
--- TABLES
---

CREATE TABLE users (
    user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    verification_status verification_status_enum NOT NULL DEFAULT 'unverified',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE profiles (
    profile_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID UNIQUE NOT NULL,
    first_name VARCHAR(100),
    last_name VARCHAR(100),
    institution VARCHAR(255),
    research_fields TEXT[],
    research_subfields TEXT[],
    research_subfields_other TEXT[],
    research_topic TEXT,
    use_case TEXT,
    estimated_usage TEXT,
    professional_links TEXT,
    attestation_type attestation_type_enum,
    country VARCHAR(100),
    city VARCHAR(100),
    region VARCHAR(100),
    us_state VARCHAR(2),
    collaborator_name VARCHAR(200),
    collaborator_email VARCHAR(255),
    collaborator_institution VARCHAR(255),
    collaborator_city VARCHAR(100),
    collaborator_us_state VARCHAR(2),
    full_name_acknowledgment VARCHAR(200),
    discovery_methods TEXT[],
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- Tags applied to users' keys (e.g. `hotswapping`). What a key carries.
CREATE TABLE user_tags (
    user_tag_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(50) UNIQUE NOT NULL,
    description TEXT,
    rate_limit INTEGER,
    quota INTEGER
);

CREATE TABLE keys (
    key_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE TABLE key_user_tag_assignments (
    key_id UUID NOT NULL,
    user_tag_id UUID NOT NULL,
    PRIMARY KEY (key_id, user_tag_id),
    FOREIGN KEY (key_id) REFERENCES keys(key_id) ON DELETE CASCADE,
    FOREIGN KEY (user_tag_id) REFERENCES user_tags(user_tag_id) ON DELETE RESTRICT
);

-- Eventually this should be renamed to e.g. "gated_models".
CREATE TABLE models (
    model_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_key VARCHAR(255) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Tags applied to models. What a model requires.
CREATE TABLE model_tags (
    model_tag_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(50) UNIQUE NOT NULL,
    description TEXT
);

CREATE TABLE model_tag_assignments (
    model_id UUID NOT NULL,
    model_tag_id UUID NOT NULL,
    PRIMARY KEY (model_id, model_tag_id),
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON DELETE CASCADE,
    FOREIGN KEY (model_tag_id) REFERENCES model_tags(model_tag_id) ON DELETE RESTRICT
);

CREATE TABLE audit_logs (
    log_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action audit_action_enum NOT NULL,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    db_user NAME NOT NULL DEFAULT current_user,
    affected_id UUID
);

---
--- INDEXES
---

CREATE INDEX idx_keys_key_id ON keys(key_id);
CREATE INDEX idx_users_email ON users(email);

---
--- ROLES
---
-- The API connects as `ndifapi` (read-only) to verify keys; the account portal
-- connects as `login_page` to manage users/keys. Passwords are dev-only.

CREATE ROLE readonly;
GRANT USAGE ON SCHEMA public TO readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO readonly;

CREATE USER ndifapi WITH PASSWORD 'admin';
GRANT readonly TO ndifapi;

CREATE USER login_page WITH PASSWORD 'admin';
GRANT readonly TO login_page;
GRANT INSERT, UPDATE, DELETE ON TABLE keys TO login_page;
GRANT INSERT ON TABLE users TO login_page;

-- No seed data: no default users or API keys are created.
