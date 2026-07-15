-- ---------------------------------------------------------------------------
-- Run this in the Supabase SQL editor BEFORE you point Lovable at the database.
-- Your anon key ships to every visitor's browser. Without RLS it is a public
-- write key to your entire table.
-- ---------------------------------------------------------------------------

alter table opportunities add column if not exists apply_url    text;
alter table opportunities add column if not exists link_type    text default 'portal_apply';
alter table opportunities add column if not exists logo_url     text;
alter table opportunities add column if not exists is_live      boolean not null default true;
alter table opportunities add column if not exists last_seen_at timestamptz default now();

alter table opportunities enable row level security;

-- Visitors: read live rows. Nothing else.
drop policy if exists "public read live" on opportunities;
create policy "public read live"
  on opportunities for select
  to anon
  using (is_live = true);

-- No insert/update/delete policy for anon = those are denied by default.
-- The bot uses the service_role key, which bypasses RLS entirely.

create index if not exists opportunities_live_idx on opportunities (is_live, status, start_date);
