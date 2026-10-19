# AutoCAD submissions: integration map

This document records how the first AutoCAD upload page fits into the existing Student Analytics app, so future assignment, preview, and grading work can extend it without bypassing current authentication or persistence paths.

## Existing application flow

```text
Browser request
  → FastAPI route in app/main.py
  → auth_gate middleware in app/main.py
      → session identity from app/auth.py
      → URL prefix mapped to a page in app/auth.py
      → role and onboarding-status access check
  → page handler builds the shared sidebar/context
  → Jinja template in app/templates/pages
  → Neon Postgres (DATABASE_URL) or local SQLite fallback
```

- `app/main.py` owns FastAPI routes, the shared sidebar list and icons, `_base_context`, upload endpoints, downloads, and audit events.
- `app/auth.py` maps URL prefixes to navigation pages, defines `ROLE_PAGES`, and narrows student access while onboarding is pending or rejected. AutoCAD is available to students, faculty, and admins who have passed the existing student onboarding gate.
- `app/templates/base.html` renders the shared navigation. Page-specific content extends this template.
- `app/database.py` selects Neon Postgres when configured; otherwise the app uses feature-specific SQLite stores for local development.
- `app/db.py` contains Postgres query helpers. `app/schema.sql` is the idempotent Postgres schema; startup applies it when the schema file hash changes.
- Existing uploads are not a single shared storage mechanism: support and certificate uploads have separate modules and tables. They remain unchanged by AutoCAD.

## Other current flows that share this foundation

- **Student onboarding:** sign-up creates a student account; students submit academic identity details; staff review the onboarding ledger. Pending/rejected students are deliberately kept on Onboarding, Support, and Settings until approved.
- **Analytics:** approved accounts are synchronized from GitHub into account snapshots. Scheduled Vercel and GitHub Actions jobs refresh account and HackerRank data; page payloads are assembled in `app/views.py` and cached through `app/view_cache.py`.
- **Certificates:** students submit credential details and small file proofs from their profile; administrators approve or reject those records in `/verification`. This is a separate workflow from CAD submissions.
- **Support:** students and staff exchange support tickets, replies, and image attachments; notifications and the sidebar bell use the support notification storage.
- **Faculty scope:** AutoCAD uses the same faculty teaching map as the Repositories page when one is configured. The role model remains broader than per-course access, and the **All students** scope is intentionally available to faculty just as it is on Repositories. Tighter access should wait for a clear course/assignment ownership model.
- **Hosting:** `vercel.json` routes the FastAPI app through a Vercel Python Function. `DATABASE_URL` selects Neon in production; SQLite is the local fallback. Do not rely on a local uploaded file being durable on Vercel.

## AutoCAD MVP flow

```text
Student /autocad page
  → POST /autocad/upload
  → validate role, extension, size, and DWG signature
  → app/autocad.py persistence abstraction
      → app/db.py + Postgres BYTEA in production
      → autocad.db SQLite file for local development
  → redirect back to /autocad

Faculty/admin /autocad page
  → latest 500 submissions
  → Repositories-style class/semester/search filters
  → GET /autocad/submissions/{id}/file
  → ownership-or-staff authorization
  → attachment download
```

The current upload limit is **4 MiB**. DWG file contents are stored with their metadata in `autocad_submissions`; the browser never supplies the student email or PRN. Those identity values come from the signed-in account and its account row. The stored file is only returned by an authenticated download route. A student's list is filtered to that student's email. Staff see up to the latest 500 uploads; the Division/Batch picker, Semester select, and search filter that list. Faculty with teaching assignments start in **My Classes** scope and can switch to **All students**, matching the Repositories page; admins can filter across all classes.

The page supports a basic student upload and faculty download inbox. It does not yet create assignments, render DWG files in a browser, allow deletion, or record grades and remarks. Each upload is an independent row, so repeated uploads do not overwrite earlier files. Class filtering currently reads the student's current division, practical batch, and semester from their account record; future assignments should store the assigned cohort on the assignment/submission so class changes cannot move historical work into a different class.

## Why the initial limit and storage choice are small

Vercel Functions have a 4.5 MB request-body limit. The file cap is set below that at 4 MiB to leave room for the multipart form envelope. The three example DWGs provided for this feature are each under 64 KB. Larger real-world files need a direct-to-object-storage upload path (for example, private Vercel Blob with a short-lived server-issued upload token) before the limit is raised.

Postgres `BYTEA` and the local SQLite BLOB make the first pass self-contained and use the app's existing persistence modes. They are a transitional choice for a growing CAD file library: database backups and storage usage will grow with every upload, and Vercel's function response path is not intended to act as a large-file media server. The download endpoint currently buffers one file in memory, which is bounded by the upload limit.

## Safe extension points

1. Add assignment records and associate each submission with an assignment ID; preserve the student identity fields and keep authorization checks in the route and persistence query.
2. Add a faculty-to-course/class mapping before restricting or delegating grading. Until that relationship exists, faculty can see all submissions.
3. Keep submission versions immutable. Store grades and remarks against the reviewed submission (or in a separate review history) so later resubmissions do not erase prior feedback.
4. For in-browser CAD review, add private-file authorization and an Autodesk Platform Services translation/viewer workflow. Do not expose storage credentials or make student DWGs public to embed a viewer.
5. If files grow beyond the MVP limit, upload directly to private object storage and keep only file metadata plus a storage key in Postgres/SQLite. Validate the completed upload server-side before making it visible to faculty.
6. Add retention/deletion policy, upload quotas, and optional malware scanning before accepting high-volume or externally shared files.

## Existing behaviors this feature must preserve

- Middleware remains the source of truth for login and page access; hiding a nav item alone is not authorization.
- Pending or rejected students remain restricted to the existing Onboarding, Support, and Settings pages; AutoCAD is not added to that exception list.
- Student submissions must be read using the authenticated student's account identity. Staff-only cross-student access is explicit in the file endpoint.
- Production database writes continue through `app/db.py`; local fallback writes continue through the feature module's SQLite store.
- File names are reduced to a safe basename, file type is checked using the DWG release signature rather than browser MIME metadata, and downloads are marked as attachments with `nosniff` and private no-store cache headers.
- Existing support, certificates, GitHub sync, notifications, and analytics flows remain separate and are not reused for AutoCAD file bytes.
