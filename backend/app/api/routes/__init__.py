# Intentionally no eager submodule imports: the slim exam API image
# (requirements-exams.txt) must be able to import `app.api.routes.exams_*`
# without pulling in the legacy search stack (numpy/FAISS/spaCy). Callers use
# `from app.api.routes import <module>`, which imports the submodule on demand.
