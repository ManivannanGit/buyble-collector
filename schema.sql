-- Buyble collector schema (Supabase Postgres). Safe to re-run.

create table if not exists raw_posts (
  id text primary key,                 -- source:native_id
  source text not null,                -- 'reddit'
  url text not null,
  subreddit text,
  author_hash text,
  created_at timestamptz,
  title text, body text,               -- trimmed, max 4000 chars, deleted after 30 days
  item_key text not null,              -- items.yaml key that found it
  fetched_at timestamptz default now(),
  extracted boolean default false
);

create table if not exists points (
  id bigserial primary key,
  status text not null default 'staged' check (status in ('staged','approved','rejected')),
  source text not null,                -- 'reddit' | 'bill' | 'buyer' | later 'maps','instagram'
  raw_post_id text references raw_posts(id),
  url text,
  kind text not null check (kind in ('purchase','asked','component','service')),
  line_kind text check (line_kind in ('part','labour','service_package','food','product')),
  item text not null,                  -- as written, max 80 chars
  canonical text not null,             -- generic name, lowercase, 1-3 words
  category text not null,              -- food_drink | apparel | auto_parts | electronics | handmade | service | other
  attrs jsonb not null default '{}',   -- composition facts + channel
  attr_source text not null default 'unknown',  -- verified | seller_stated | buyer_stated | inferred | unknown
  price numeric not null check (price > 0),
  currency text not null default 'INR',
  unit text,                           -- '1 cup', '250 g', '1 pc', '8 km'
  price_per_unit numeric,
  unit_basis text,                     -- 'cup','100g','pc','km','job'
  city text,
  quality smallint check (quality between 1 and 5),
  worth text check (worth in ('good_deal','fair','pricey','overpriced')),
  known boolean default false,
  quote text,                          -- <= 300 chars, verbatim from source
  for_model text,
  author_hash text,
  purchased_at date,
  trust numeric not null default 1.0,
  confidence numeric,
  created_at timestamptz default now(),
  reviewed_at timestamptz,
  review_note text
);
create index if not exists points_canonical on points(canonical, status);
create unique index if not exists points_dedupe on points(raw_post_id, canonical, price);

create table if not exists attr_maps (
  canonical text primary key,
  category text not null,
  attrs jsonb not null,                -- [{key,label,question,options:[{value,label,note}]}]
  approved boolean default false,
  updated_at timestamptz default now()
);

create table if not exists golden (
  id bigserial primary key,
  item text not null, price numeric not null, city text,
  attrs jsonb default '{}',
  expected text not null check (expected in ('good_deal','fair','pricey','overpriced')),
  note text, created_at timestamptz default now()
);

create table if not exists runs (
  id bigserial primary key,
  started_at timestamptz default now(), finished_at timestamptz,
  posts_fetched int default 0, llm_calls int default 0,
  points_staged int default 0, errors jsonb default '[]'
);
alter table runs add column if not exists kind text default 'collect';   -- 'collect' | 'golden'
alter table runs add column if not exists golden_accuracy numeric;

-- Supabase exposes public tables over its REST API; with RLS on and no policies,
-- only the owner role (which the collector uses) can read or write.
alter table raw_posts enable row level security;
alter table points    enable row level security;
alter table attr_maps enable row level security;
alter table golden    enable row level security;
alter table runs      enable row level security;
