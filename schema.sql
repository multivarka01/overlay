-- Run once in Supabase: SQL Editor -> New query -> Run
create table if not exists public.kv (
  key        text primary key,
  value      text not null,
  updated_at timestamptz not null default now()
);

-- RLS on and no policies: only the secret/service key can read or write this table.
alter table public.kv enable row level security;
