-- =====================================================================================
-- !!! Reconstructed from client code, not exported from the real project.
-- !!! Compare with your actual database before applying.
-- =====================================================================================
-- Восстановлено по клиентскому коду, а не выгружено из настоящего проекта.
-- Сравните с вашей базой данных, прежде чем применять.
--
-- What the code (index.html / app.html / studio.html) uses from the backend:
--   * table  public.scenes          select/insert/update/delete by the owner; the name is a streamer nick
--   * rpc    get_overlay_scene(k)   anonymous read of a scene by its secret overlay key (OBS)
--   * rpc    rotate_overlay_key(sid) new key for the caller's own row ("Новая ссылка")
--   * realtime private channel      topic 'ov:<overlay_key>' (owner sends, key holder receives)
--   * storage bucket 'images'       public, uploaded as '<uid>/<file>', read through getPublicUrl
--   * auth                          Twitch provider (configure in the Dashboard, not in SQL)
--
-- Run in the Supabase SQL Editor. It is written to be re-runnable (create or replace / drop if exists).
-- =====================================================================================

-- ---------- 1. Table ----------------------------------------------------------------
create extension if not exists pgcrypto;  -- gen_random_uuid() (built in on current Postgres, harmless otherwise)

create table if not exists public.scenes (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null default auth.uid() references auth.users (id) on delete cascade,
  name        text not null check (char_length(name) between 1 and 40),
  data        jsonb not null default '[]'::jsonb,
  overlay_key uuid not null default gen_random_uuid() unique,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (user_id, name)
);

create index if not exists scenes_user_created_idx on public.scenes (user_id, created_at);

alter table public.scenes enable row level security;

-- ---------- 2. Privileges -----------------------------------------------------------
-- anon never touches the table directly; it reads a scene only through get_overlay_scene().
revoke all on public.scenes from anon;
revoke all on public.scenes from authenticated;
grant select, insert, delete on public.scenes to authenticated;
-- overlay_key / user_id / id cannot be changed by the client: rotation goes through rotate_overlay_key().
grant update (name, data, updated_at) on public.scenes to authenticated;

-- ---------- 3. RLS policies (owner only, max 10 rows per user on insert) ------------
drop policy if exists "scenes_select_own" on public.scenes;
create policy "scenes_select_own" on public.scenes
  for select to authenticated
  using (user_id = (select auth.uid()));

-- The client treats an error matching /row-level security/ on insert as "maximum 10 streamers".
-- A failed WITH CHECK raises: 'new row violates row-level security policy for table "scenes"'.
drop policy if exists "scenes_insert_own_limited" on public.scenes;
create policy "scenes_insert_own_limited" on public.scenes
  for insert to authenticated
  with check (
    user_id = (select auth.uid())
    and (select count(*) from public.scenes s where s.user_id = (select auth.uid())) < 10
  );

drop policy if exists "scenes_update_own" on public.scenes;
create policy "scenes_update_own" on public.scenes
  for update to authenticated
  using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));

drop policy if exists "scenes_delete_own" on public.scenes;
create policy "scenes_delete_own" on public.scenes
  for delete to authenticated
  using (user_id = (select auth.uid()));

-- ---------- 4. Functions ------------------------------------------------------------
-- Anonymous read for OBS: whoever knows the key gets the scene JSON (an array of elements).
create or replace function public.get_overlay_scene(k uuid)
returns jsonb
language sql
stable
security definer
set search_path = ''
as $$
  select s.data from public.scenes s where s.overlay_key = k limit 1;
$$;

-- "Новая ссылка": new key for the caller's own row only. Returns the new key.
create or replace function public.rotate_overlay_key(sid uuid)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  new_key uuid := gen_random_uuid();
begin
  update public.scenes
     set overlay_key = new_key, updated_at = now()
   where id = sid and user_id = (select auth.uid());
  if not found then
    raise exception 'scene not found' using errcode = 'P0002';
  end if;
  return new_key;
end;
$$;

-- Helper for the realtime policy: does a scene with this key exist? (RLS on scenes would hide it from anon.)
create or replace function public.overlay_key_exists(k uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (select 1 from public.scenes s where s.overlay_key = k);
$$;

revoke all on function public.get_overlay_scene(uuid)   from public;
revoke all on function public.rotate_overlay_key(uuid)  from public;
revoke all on function public.overlay_key_exists(uuid)  from public;
grant execute on function public.get_overlay_scene(uuid)  to anon, authenticated;
grant execute on function public.rotate_overlay_key(uuid) to authenticated;
grant execute on function public.overlay_key_exists(uuid) to anon, authenticated;

-- ---------- 5. Realtime authorization for private channels 'ov:<overlay_key>' --------
-- The client opens: supabase.channel('ov:' || key, { config: { private: true } }).
-- Policies live on realtime.messages; realtime.topic() returns the channel topic.
--
-- Trade-offs:
--   * Receiving is allowed to ANY role that knows an existing key (OBS runs as anon). The key is the
--     secret; rotate it if it leaks. Everything sent to the channel is visible to key holders.
--   * Sending is allowed only to the owner of that scene, so a key holder cannot push fake updates.
--   * Policies are evaluated when a client joins a channel and are cached for the connection, so after
--     rotating a key an already connected OBS keeps receiving until it reconnects.
--   * Enable "Allow public access" OFF in Realtime settings so that only these policies apply.
drop policy if exists "ov_receive_by_key" on realtime.messages;
create policy "ov_receive_by_key" on realtime.messages
  for select to anon, authenticated
  using (
    realtime.messages.extension = 'broadcast'
    and (select realtime.topic()) like 'ov:%'
    and public.overlay_key_exists(
          case when substring((select realtime.topic()) from 4)
                    ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               then substring((select realtime.topic()) from 4)::uuid end)
  );

drop policy if exists "ov_send_by_owner" on realtime.messages;
create policy "ov_send_by_owner" on realtime.messages
  for insert to authenticated
  with check (
    realtime.messages.extension = 'broadcast'
    and exists (
      select 1 from public.scenes s
      where s.user_id = (select auth.uid())
        and 'ov:' || s.overlay_key::text = (select realtime.topic())
    )
  );

-- ---------- 6. Storage: bucket 'images' ---------------------------------------------
-- Public bucket (files are served through getPublicUrl), 3 MB limit, only common image types.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('images', 'images', true, 3145728, array['image/png', 'image/jpeg', 'image/webp', 'image/gif'])
on conflict (id) do update
  set public = excluded.public,
      file_size_limit = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;

-- Authenticated users may upload only under their own '<uid>/' folder.
-- Reading public objects does not need a SELECT policy. No update/delete policies are defined,
-- so users cannot overwrite or remove files (add own-folder policies if you want clean-up).
drop policy if exists "images_insert_own_folder" on storage.objects;
create policy "images_insert_own_folder" on storage.objects
  for insert to authenticated
  with check (
    bucket_id = 'images'
    and (storage.foldername(name))[1] = (select auth.uid())::text
  );
