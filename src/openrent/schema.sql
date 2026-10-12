-- Monetary amounts are integer pence. Booleans are nullable 0/1 values:
-- NULL means that the listing did not disclose the fact.
CREATE TABLE IF NOT EXISTS properties (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    url TEXT NOT NULL CHECK (length(url) > 0),
    title TEXT,
    description TEXT,
    description_html TEXT,
    property_type TEXT,
    property_type_code INTEGER,
    address_display TEXT,
    locality TEXT,
    postcode TEXT,
    country TEXT NOT NULL DEFAULT 'GB',
    latitude REAL CHECK (latitude BETWEEN -90 AND 90),
    longitude REAL CHECK (longitude BETWEEN -180 AND 180),
    openrent_commute_minutes REAL CHECK (openrent_commute_minutes >= 0),
    search_target TEXT,
    search_target_latitude REAL CHECK (search_target_latitude BETWEEN -90 AND 90),
    search_target_longitude REAL CHECK (search_target_longitude BETWEEN -180 AND 180),
    bedrooms INTEGER CHECK (bedrooms >= 0),
    bathrooms INTEGER CHECK (bathrooms >= 0),
    max_tenants INTEGER CHECK (max_tenants >= 0),
    rent_pcm_pence INTEGER CHECK (rent_pcm_pence >= 0),
    rent_weekly_pence INTEGER CHECK (rent_weekly_pence >= 0),
    deposit_pence INTEGER CHECK (deposit_pence >= 0),
    currency TEXT NOT NULL DEFAULT 'GBP' CHECK (length(currency) = 3),
    available_from TEXT,
    minimum_tenancy_months INTEGER CHECK (minimum_tenancy_months >= 0),
    maximum_tenancy_months INTEGER CHECK (maximum_tenancy_months >= 0),
    furnished INTEGER CHECK (furnished IN (0, 1)),
    unfurnished INTEGER CHECK (unfurnished IN (0, 1)),
    furnishing TEXT,
    bills_included INTEGER CHECK (bills_included IN (0, 1)),
    pets_allowed INTEGER CHECK (pets_allowed IN (0, 1)),
    students_allowed INTEGER CHECK (students_allowed IN (0, 1)),
    non_students_allowed INTEGER CHECK (non_students_allowed IN (0, 1)),
    families_allowed INTEGER CHECK (families_allowed IN (0, 1)),
    dss_covers_rent INTEGER CHECK (dss_covers_rent IN (0, 1)),
    garden INTEGER CHECK (garden IN (0, 1)),
    parking INTEGER CHECK (parking IN (0, 1)),
    fireplace INTEGER CHECK (fireplace IN (0, 1)),
    smokers_allowed INTEGER CHECK (smokers_allowed IN (0, 1)),
    has_video INTEGER CHECK (has_video IN (0, 1)),
    video_viewings INTEGER CHECK (video_viewings IN (0, 1)),
    is_shared INTEGER CHECK (is_shared IN (0, 1)),
    is_studio INTEGER CHECK (is_studio IN (0, 1)),
    is_live INTEGER CHECK (is_live IN (0, 1)),
    status TEXT,
    first_listed_at TEXT,
    epc_rating TEXT,
    landlord_name TEXT,
    landlord_member_since TEXT,
    landlord_last_active TEXT,
    source_html TEXT,
    extra_metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(extra_metadata_json)),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS properties_location ON properties(latitude, longitude);
CREATE INDEX IF NOT EXISTS properties_postcode ON properties(postcode);
CREATE INDEX IF NOT EXISTS properties_price_bedrooms ON properties(rent_pcm_pence, bedrooms);
CREATE INDEX IF NOT EXISTS properties_available ON properties(is_live, available_from);

-- One copy of each image's actual bytes, even if several properties use it.
CREATE TABLE IF NOT EXISTS image_blobs (
    sha256 TEXT PRIMARY KEY CHECK (length(sha256) = 64),
    content BLOB NOT NULL CHECK (length(content) > 0),
    byte_length INTEGER NOT NULL CHECK (byte_length = length(content)),
    content_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS property_images (
    property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    source_url TEXT NOT NULL CHECK (length(source_url) > 0),
    position INTEGER NOT NULL DEFAULT 0 CHECK (position >= 0),
    kind TEXT NOT NULL DEFAULT 'photo',
    caption TEXT,
    width INTEGER CHECK (width > 0),
    height INTEGER CHECK (height > 0),
    content_sha256 TEXT REFERENCES image_blobs(sha256),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (property_id, source_url)
);
CREATE INDEX IF NOT EXISTS property_images_order ON property_images(property_id, position);
CREATE INDEX IF NOT EXISTS property_images_blob ON property_images(content_sha256);

-- Typed scalar feature rows keep uncommon amenities queryable without JSON.
CREATE TABLE IF NOT EXISTS property_features (
    property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    section TEXT NOT NULL DEFAULT '',
    feature_key TEXT NOT NULL,
    label TEXT NOT NULL,
    value_type TEXT CHECK (value_type IN ('text', 'integer', 'real', 'boolean')),
    value_text TEXT,
    value_integer INTEGER,
    value_real REAL,
    value_boolean INTEGER CHECK (value_boolean IN (0, 1)),
    unit TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (property_id, section, feature_key),
    CHECK (
        (value_type IS NULL AND value_text IS NULL AND value_integer IS NULL
            AND value_real IS NULL AND value_boolean IS NULL)
        OR (value_type = 'text' AND value_text IS NOT NULL AND value_integer IS NULL
            AND value_real IS NULL AND value_boolean IS NULL)
        OR (value_type = 'integer' AND value_text IS NULL AND value_integer IS NOT NULL
            AND value_real IS NULL AND value_boolean IS NULL)
        OR (value_type = 'real' AND value_text IS NULL AND value_integer IS NULL
            AND value_real IS NOT NULL AND value_boolean IS NULL)
        OR (value_type = 'boolean' AND value_text IS NULL AND value_integer IS NULL
            AND value_real IS NULL AND value_boolean IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS property_features_key ON property_features(feature_key, value_boolean);

CREATE TABLE IF NOT EXISTS nearby_places (
    property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    walking_minutes INTEGER CHECK (walking_minutes >= 0),
    distance_km REAL CHECK (distance_km >= 0),
    latitude REAL CHECK (latitude BETWEEN -90 AND 90),
    longitude REAL CHECK (longitude BETWEEN -180 AND 180),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (property_id, name, kind)
);
CREATE INDEX IF NOT EXISTS nearby_places_name ON nearby_places(name, kind);

-- One independently fetched JSON payload per property and amenity type.
CREATE TABLE IF NOT EXISTS amenities (
    property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    data BLOB NOT NULL CHECK (typeof(data) = 'blob' AND json_valid(data, 8)),
    PRIMARY KEY (property_id, type)
);

CREATE TABLE IF NOT EXISTS media_links (
    property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    kind TEXT NOT NULL,
    caption TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (property_id, url, kind)
);

DROP VIEW IF EXISTS property_summary;

-- Completed reviews and notification delivery history live in the archive.
CREATE TABLE IF NOT EXISTS review_profiles (
    profile_key TEXT PRIMARY KEY CHECK (length(profile_key) = 64),
    criteria TEXT NOT NULL CHECK (length(criteria) > 0),
    model TEXT NOT NULL CHECK (length(model) > 0),
    backend TEXT NOT NULL DEFAULT 'codex' CHECK (backend IN ('codex', 'responses')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS property_reviews (
    id INTEGER PRIMARY KEY,
    profile_key TEXT NOT NULL REFERENCES review_profiles(profile_key),
    property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL CHECK (length(fingerprint) = 64),
    decision TEXT GENERATED ALWAYS AS (json_extract(result, '$.decision')) VIRTUAL
        CHECK (decision IN ('pass', 'reject', 'uncertain')),
    summary TEXT GENERATED ALWAYS AS (json_extract(result, '$.summary')) VIRTUAL,
    result BLOB NOT NULL CHECK (
        typeof(result) = 'blob' AND json_valid(result, 8) AND json_type(result) = 'object'
    ),
    processed_at TEXT NOT NULL,
    reviewed_title TEXT,
    reviewed_url TEXT NOT NULL,
    reviewed_address_display TEXT,
    reviewed_rent_pcm_pence INTEGER CHECK (reviewed_rent_pcm_pence >= 0),
    reviewed_bedrooms INTEGER CHECK (reviewed_bedrooms >= 0),
    UNIQUE (profile_key, property_id, fingerprint),
    CHECK (decision IS NOT NULL AND summary IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS property_reviews_processed
    ON property_reviews(property_id);
CREATE INDEX IF NOT EXISTS property_reviews_profile
    ON property_reviews(profile_key, decision);

-- The exact judged gallery remains available even after a scanner replaces it.
-- Image bytes are shared with the property gallery.
CREATE TABLE IF NOT EXISTS review_images (
    review_id INTEGER NOT NULL REFERENCES property_reviews(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    source_position INTEGER NOT NULL CHECK (source_position >= 0),
    sha256 TEXT NOT NULL REFERENCES image_blobs(sha256) CHECK (length(sha256) = 64),
    source_url TEXT NOT NULL,
    kind TEXT NOT NULL,
    caption TEXT,
    PRIMARY KEY (review_id, position)
);
CREATE INDEX IF NOT EXISTS review_images_blob ON review_images(sha256);

-- Review completion enqueues a prepared digest item, without image bytes.
CREATE TABLE IF NOT EXISTS notification_outbox (
    property_id INTEGER PRIMARY KEY,
    review_id INTEGER NOT NULL REFERENCES property_reviews(id) ON DELETE CASCADE,
    payload BLOB NOT NULL CHECK (typeof(payload) = 'blob' AND json_valid(payload, 8))
);

-- Notification history deliberately has no foreign key to an archive property
-- or review. Removing a property must never make its ID eligible for another
-- notification to the same recipient.
CREATE TABLE IF NOT EXISTS email_batches (
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
CREATE INDEX IF NOT EXISTS email_batches_recipient_status
    ON email_batches(recipient, status, id);

CREATE TABLE IF NOT EXISTS email_batch_items (
    batch_id INTEGER NOT NULL,
    recipient TEXT NOT NULL,
    property_id INTEGER NOT NULL CHECK (property_id > 0),
    review_id INTEGER NOT NULL,
    PRIMARY KEY (batch_id, property_id),
    UNIQUE (recipient, property_id),
    FOREIGN KEY (batch_id, recipient) REFERENCES email_batches(id, recipient)
);
