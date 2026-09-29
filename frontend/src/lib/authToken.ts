import { auth } from "@/lib/firebase";
import { E2E_AUTH_BYPASS, readE2EToken } from "@/lib/e2eAuth";

export async function getClientAuthToken(): Promise<string | null> {
  if (auth.currentUser) {
    return auth.currentUser.getIdToken();
  }

  if (E2E_AUTH_BYPASS) {
    const token = readE2EToken();
    return token || null;
  }

  return null;
}
