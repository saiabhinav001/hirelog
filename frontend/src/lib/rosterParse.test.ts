import { describe, expect, it } from "vitest";

import { parseRoster } from "@/lib/rosterParse";

describe("parseRoster", () => {
  it("reads comma, semicolon and tab separated rows and defaults the role to student", () => {
    const { rows, problems } = parseRoster("21CS001, a@college.edu\n21CS002;b@college.edu;Faculty\n21CS003\tc@college.edu");
    expect(problems).toEqual([]);
    expect(rows).toEqual([
      { identity_key: "21CS001", contact_email: "a@college.edu", role_scope: "student" },
      { identity_key: "21CS002", contact_email: "b@college.edu", role_scope: "faculty" },
      { identity_key: "21CS003", contact_email: "c@college.edu", role_scope: "student" },
    ]);
  });

  it("skips a header row and blank lines but not later rows that merely look like headers", () => {
    const { rows } = parseRoster("college_id,email,role\n\n21CS001,a@college.edu\nid,odd@college.edu");
    expect(rows.map((row) => row.identity_key)).toEqual(["21CS001", "id"]);
  });

  it("reports unreadable lines with their line number instead of dropping them silently", () => {
    const { rows, problems } = parseRoster("21CS001,a@college.edu\njust-an-id\n,missing-id@college.edu");
    expect(rows).toHaveLength(1);
    expect(problems).toEqual([
      'Line 2: expected "college ID, email" (optionally ", role").',
      'Line 3: expected "college ID, email" (optionally ", role").',
    ]);
  });
});
