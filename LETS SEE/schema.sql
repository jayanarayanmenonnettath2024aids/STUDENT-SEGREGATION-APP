-- Run in Supabase SQL Editor
create table if not exists public.students (
  id bigserial primary key,
  roll varchar(20) not null,
  name text not null,
  dept text,
  sec text,
  category varchar(2),
  remarks text,
  saved_at timestamp,
  saved_by varchar(100)
);
create table if not exists mentors (
    id uuid default gen_random_uuid() primary key,
    username text unique not null,
    password text not null,
    dept text
);