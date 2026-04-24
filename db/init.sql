CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    eth_id VARCHAR(64) UNIQUE NOT NULL,
    email VARCHAR(255) NOT NULL,
    first_name VARCHAR(128),
    last_name VARCHAR(128),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tokens (
    id SERIAL PRIMARY KEY,
    token VARCHAR(255) UNIQUE NOT NULL,
    label VARCHAR(255),
    is_default BOOLEAN DEFAULT FALSE,
    user_id INTEGER UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tasks (
    uid UUID PRIMARY KEY,
    token_id INTEGER REFERENCES tokens(id) NOT NULL,
    state VARCHAR(2) NOT NULL DEFAULT 'PD',
    n_samples INTEGER,
    created_at TIMESTAMP DEFAULT NOW(),
    done_at TIMESTAMP,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS logs (
    id SERIAL PRIMARY KEY,
    token_id INTEGER REFERENCES tokens(id),
    eth_id VARCHAR(64),
    action VARCHAR(50) NOT NULL,
    n_samples INTEGER,
    detail TEXT,
    ip VARCHAR(45),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS server_state (
    id INTEGER PRIMARY KEY,
    state VARCHAR(16) NOT NULL DEFAULT 'running',
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Ensure a single state row
INSERT INTO server_state (id, state) VALUES (1, 'running')
    ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS setting_overrides (
    key VARCHAR(64) PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_logs_created_at ON logs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_logs_token_id ON logs (token_id);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks (created_at DESC);
