create extension if not exists pgcrypto;
create extension if not exists pgtap;
do $$ declare r text; begin
  foreach r in array array['anon','authenticated','service_role','tc_one_bot','sva_app'] loop
    if not exists (select 1 from pg_roles where rolname=r) then execute format('create role %I nologin noinherit', r); end if;
  end loop;
  if not exists (select 1 from pg_roles where rolname='authenticator') then create role authenticator login noinherit; end if;
end $$;
create schema identity;
create table identity.countries (code text primary key);
insert into identity.countries values ('SG'),('MY'),('HK'),('TH');
create table identity.people (
  id uuid primary key default gen_random_uuid(), full_name text not null, telegram_id bigint unique, telegram_username text,
  email text unique, manager_id uuid references identity.people(id), country_code text references identity.countries(code),
  person_type text not null default 'staff' check (person_type in ('staff','freelancer','promoter','intern','guest')),
  status text not null default 'active' check (status in ('active','pending','revoked')),
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(), nickname text,
  employment_type text check (employment_type in ('full_time_staff','part_time_staff','full_time_intern','part_time_intern','freelancer','promoter')));
create function identity.people_employment_type_sync() returns trigger language plpgsql as $f$
declare v_type text; begin
  v_type := case new.employment_type when 'full_time_staff' then 'staff' when 'part_time_staff' then 'staff'
    when 'full_time_intern' then 'intern' when 'part_time_intern' then 'intern' when 'freelancer' then 'freelancer' when 'promoter' then 'promoter' end;
  if new.employment_type is not null and (tg_op='INSERT' or coalesce(new.employment_type,'')<>coalesce(old.employment_type,'')) then
    new.person_type := coalesce(v_type, new.person_type); end if;
  return new; end $f$;
create trigger people_employment_type_sync before insert or update of employment_type, person_type on identity.people
  for each row execute function identity.people_employment_type_sync();
alter table identity.people enable row level security;
create schema sts;
create table sts.workloads (workload text primary key, role text not null, audiences text[] not null default '{}');
