# Student Segregator (Flask + Supabase)

## Quick Start
1) Create a Supabase project. In SQL editor, run `schema.sql`.
2) Copy `.env.example` to `.env` and fill `SUPABASE_URL` + `SUPABASE_KEY` (service role key).
3) Put your single-sheet Excel in `data/FIRST YEARS.xlsx` with columns: `S.NO, NAME, DEPT, SEC`.
4) Create and activate a venv, then:
   ```bash
   pip install -r requirements.txt
   python app.py
   ```
5) Visit http://localhost:5000

### Logins
- Admin: `ADMIN_MENTORING` / `MENTORING123`
- Mentor (demo): `mentor_demo` / `mentor123`

### Notes
- Category (A/B/C) is **auto-calculated** from radio inputs for 6 dimensions.
- Remarks store selected jargon codes (comma-separated) + any free-text note.
- Admin can export **filtered** view to Excel from their dashboard.
