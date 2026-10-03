-- This file is executed after the sidecar is attached with the alias review.
CREATE TABLE IF NOT EXISTS review.review_profiles (
    profile_key TEXT PRIMARY KEY CHECK (length(profile_key) = 64),
    criteria TEXT NOT NULL CHECK (length(criteria) > 0),
    model TEXT NOT NULL CHECK (length(model) > 0),
    backend TEXT NOT NULL DEFAULT 'codex' CHECK (backend IN ('codex', 'responses')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review.property_reviews (
    id INTEGER PRIMARY KEY,
    profile_key TEXT NOT NULL REFERENCES review_profiles(profile_key),
    -- SQLite cannot enforce a foreign key across attached databases. Archive
    -- property references are validated and deleted by the application.
    property_id INTEGER NOT NULL CHECK (property_id > 0),
    fingerprint TEXT NOT NULL CHECK (length(fingerprint) = 64),
    status TEXT NOT NULL CHECK (status IN ('processing', 'complete', 'error')),
    decision TEXT GENERATED ALWAYS AS (json_extract(result, '$.decision')) VIRTUAL
        CHECK (decision IN ('pass', 'reject', 'uncertain')),
    summary TEXT GENERATED ALWAYS AS (json_extract(result, '$.summary')) VIRTUAL,
    result BLOB CHECK (result IS NULL OR (
        typeof(result) = 'blob' AND json_valid(result, 8) AND json_type(result) = 'object'
    )),
    error TEXT,
    started_at TEXT NOT NULL,
    processed_at TEXT,
    reviewed_title TEXT,
    reviewed_url TEXT NOT NULL,
    reviewed_address_display TEXT,
    reviewed_rent_pcm_pence INTEGER CHECK (reviewed_rent_pcm_pence >= 0),
    reviewed_bedrooms INTEGER CHECK (reviewed_bedrooms >= 0),
    UNIQUE (profile_key, property_id, fingerprint),
    CHECK ((status = 'complete') = (processed_at IS NOT NULL)),
    CHECK (status != 'complete' OR (
        decision IS NOT NULL AND summary IS NOT NULL AND result IS NOT NULL
    ))
);
CREATE INDEX IF NOT EXISTS review.property_reviews_processed
    ON property_reviews(property_id, status);
CREATE INDEX IF NOT EXISTS review.property_reviews_profile
    ON property_reviews(profile_key, status, decision);

-- The exact judged gallery remains available even after a scanner replaces it.
-- SHA references point to main.image_blobs and are maintained explicitly.
CREATE TABLE IF NOT EXISTS review.review_images (
    review_id INTEGER NOT NULL REFERENCES property_reviews(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    source_position INTEGER NOT NULL CHECK (source_position >= 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    source_url TEXT NOT NULL,
    kind TEXT NOT NULL,
    caption TEXT,
    PRIMARY KEY (review_id, position)
);
CREATE INDEX IF NOT EXISTS review.review_images_blob ON review_images(sha256);

-- Notification history deliberately has no foreign key to an archive property
-- or a review. Removing a property must never make its ID eligible for another
-- notification to the same recipient.
CREATE TABLE IF NOT EXISTS review.email_batches (
    id INTEGER PRIMARY KEY,
    recipient TEXT NOT NULL CHECK (length(recipient) > 0),
    sender TEXT NOT NULL CHECK (sender <> ''),
    subject TEXT NOT NULL CHECK (length(subject) > 0),
    html_body TEXT NOT NULL,
    text_body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'unknown')),
    provider_message_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    last_attempt_at TEXT,
    sent_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    UNIQUE (id, recipient),
    CHECK ((status = 'sent') = (sent_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS review.email_batches_recipient_status
    ON email_batches(recipient, status, id);

CREATE TABLE IF NOT EXISTS review.email_batch_items (
    batch_id INTEGER NOT NULL,
    recipient TEXT NOT NULL,
    property_id INTEGER NOT NULL CHECK (property_id > 0),
    review_id INTEGER NOT NULL,
    PRIMARY KEY (batch_id, property_id),
    UNIQUE (recipient, property_id),
    FOREIGN KEY (batch_id, recipient) REFERENCES email_batches(id, recipient)
);

PRAGMA review.user_version = 4;
