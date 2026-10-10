import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "@/test/axe";

describe("axe helper", () => {
  it("really detects violations (so a clean result elsewhere is meaningful)", async () => {
    const { container } = render(
      <div>
        {/* eslint-disable-next-line @next/next/no-img-element, jsx-a11y/alt-text */}
        <img src="x.png" />
        <button />
        <input />
      </div>
    );
    await expect(expectNoAxeViolations(container)).rejects.toThrow(/image-alt|button-name|label/);
  });

  it("passes a well-formed control", async () => {
    const { container } = render(
      <form>
        <label htmlFor="a">Name</label>
        <input id="a" />
        <button type="submit">Save</button>
      </form>
    );
    await expectNoAxeViolations(container);
  });
});
