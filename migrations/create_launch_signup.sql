-- Reference DDL aligned with Neon project `launch_signup` (table may already exist).

CREATE TABLE IF NOT EXISTS public.launch_signup (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email text NOT NULL,
    phone text NOT NULL,
    name text NOT NULL,
    profession text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
