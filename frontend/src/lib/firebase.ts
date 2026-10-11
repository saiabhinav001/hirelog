import { initializeApp, getApps, type FirebaseOptions } from "firebase/app";
import { connectAuthEmulator, getAuth } from "firebase/auth";

// CI and local test builds may not provide Firebase env vars.
// Use deterministic placeholder values so Next.js build does not crash.
const firebaseConfig: FirebaseOptions = {
  apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY || "demo-api-key",
  authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN || "demo.firebaseapp.com",
  projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID || "demo-project",
  storageBucket: process.env.NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET || "demo.appspot.com",
  messagingSenderId: process.env.NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID || "000000000000",
  appId: process.env.NEXT_PUBLIC_FIREBASE_APP_ID || "1:000000000000:web:demo",
};

const app = getApps().length ? getApps()[0] : initializeApp(firebaseConfig);

export const auth = getAuth(app);

// Local and CI rehearsal only: sign in against the Firebase Auth emulator instead
// of a real project. Unset in every deployed environment.
const authEmulatorUrl = process.env.NEXT_PUBLIC_FIREBASE_AUTH_EMULATOR_URL;
if (authEmulatorUrl) {
  try {
    connectAuthEmulator(auth, authEmulatorUrl, { disableWarnings: true });
  } catch {
    // Already connected (hot reload) or already used - keep the existing connection.
  }
}
// The browser never talks to Firestore directly; all data access goes through the API.
