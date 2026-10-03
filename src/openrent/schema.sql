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
    download_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (download_status IN ('pending', 'downloaded', 'error')),
    etag TEXT,
    last_modified TEXT,
    last_error TEXT,
    downloaded_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (property_id, source_url),
    CHECK ((download_status = 'downloaded') = (content_sha256 IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS property_images_order ON property_images(property_id, position);
CREATE INDEX IF NOT EXISTS property_images_blob ON property_images(content_sha256);
CREATE INDEX IF NOT EXISTS property_images_pending ON property_images(download_status);

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

CREATE VIEW IF NOT EXISTS property_summary AS
SELECT p.id, p.url, p.title, p.address_display, p.locality, p.postcode,
       p.latitude, p.longitude, p.property_type, p.bedrooms, p.bathrooms,
       p.rent_pcm_pence, p.rent_pcm_pence / 100.0 AS rent_pcm,
       p.rent_weekly_pence, p.deposit_pence, p.currency, p.available_from,
       p.furnishing, p.pets_allowed, p.bills_included, p.is_live, p.status,
       p.first_seen_at, p.last_seen_at, p.updated_at,
       (SELECT count(*) FROM property_images i WHERE i.property_id = p.id) AS image_count,
       (SELECT count(*) FROM property_images i
        WHERE i.property_id = p.id AND i.download_status = 'downloaded') AS downloaded_image_count
FROM properties p;

PRAGMA user_version = 6;
