import type { RosterRowInput } from "@/lib/examEndpoints";

const HEADER_CELL = /^(college[_ -]?id|identity[_ -]?key|student[_ -]?id|roll[_ -]?(no|number)?|id)$/i;

/**
 * Parses pasted roster text: one student per line, `college ID, email[, role]`
 * (comma, semicolon or tab separated). A header row is skipped; the role
 * defaults to "student". Returns the rows plus human-readable problems for the
 * lines that could not be read - the server still validates every row.
 */
export function parseRoster(text: string): { rows: RosterRowInput[]; problems: string[] } {
  const rows: RosterRowInput[] = [];
  const problems: string[] = [];
  const lines = text.split(/\r?\n/);
  let seenData = false;
  lines.forEach((rawLine, index) => {
    const line = rawLine.trim();
    if (!line) return;
    const cells = line.split(/[,;\t]/).map((cell) => cell.trim());
    if (!seenData && HEADER_CELL.test(cells[0] ?? "")) return;
    seenData = true;
    const [identity, email, role] = cells;
    if (!identity || !email) {
      problems.push(`Line ${index + 1}: expected "college ID, email" (optionally ", role").`);
      return;
    }
    rows.push({ identity_key: identity, contact_email: email, role_scope: (role || "student").toLowerCase() });
  });
  return { rows, problems };
}
