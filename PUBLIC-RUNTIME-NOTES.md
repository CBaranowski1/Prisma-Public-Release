# Runtime boundary of this public source

This release preserves the private branch's application behavior, not a
promise that every retained module is enabled in its UI.

`app.py` explicitly sets AUTH_ENABLED=false and removes the login UI. This is
single-user, trusted-local software with privileged tools. Use loopback or an
authenticated external access layer; AUTH_ENABLED=true is NOT an effective
protection in this branch. Shell execution is not a complete isolation boundary.

Personal organizer/email/calendar/contact route source is retained, but some
routers are deliberately not mounted in `app.py`. Their presence in the source
does not mean those features are active. Do not interpret the topology's
integration inventory as a claim that every optional module is enabled.

The public export includes the agent loop, grounded-answer pipeline, council
DAG, generic configurations, runtime skills, frontend and desktop source.
Private vault/brain corpora, live credentials and personal state are excluded.
You supply your own models and knowledge. PostgreSQL has a configurable
SQLAlchemy connection path; default SQLite tests do not prove PostgreSQL parity.
