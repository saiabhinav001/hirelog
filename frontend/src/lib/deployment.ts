/**
 * Build-time switch for a deployment that serves ONLY HireLog Exams (the
 * isolated staging project). In that mode the legacy HireLog API — including
 * its hard-coded production failover — is never contacted, the legacy profile
 * sync is skipped, and `/` lands on the institutional login.
 *
 * Unset (the default) leaves every legacy behaviour exactly as it was, so the
 * existing production deployment is unaffected.
 */
export const EXAMS_STAGING = process.env.NEXT_PUBLIC_EXAMS_STAGING === "true";
