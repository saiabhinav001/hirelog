"use client";

import {
  GoogleAuthProvider,
  User,
  createUserWithEmailAndPassword,
  onAuthStateChanged,
  signInWithEmailAndPassword,
  signInWithPopup,
  signOut as firebaseSignOut,
  updateProfile,
} from "firebase/auth";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { auth } from "@/lib/firebase";
import { apiFetch } from "@/lib/api";
import {
  E2E_AUTH_BYPASS,
  type E2ESession,
  clearE2ESession,
  readE2ESession,
  readE2EToken,
  writeE2ESession,
} from "@/lib/e2eAuth";

export type UserProfile = {
  id?: string;
  uid: string;
  name: string;
  display_name?: string;
  email: string;
  role: "viewer" | "contributor" | "placement_cell";
  created_at?: string;
  can_edit_name?: boolean;
  next_name_edit_date?: string | null;
  name_last_updated_at?: string;
};

type AuthContextValue = {
  user: User | null;
  profile: UserProfile | null;
  loading: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  signUp: (name: string, email: string, password: string) => Promise<void>;
  signInWithGoogle: () => Promise<void>;
  signOut: () => Promise<void>;
  refreshProfile: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState(true);

  const toE2EUser = useCallback((session: E2ESession): User => {
    return {
      uid: session.uid,
      email: session.email,
      displayName: session.name,
      getIdToken: async () => readE2EToken(),
    } as unknown as User;
  }, []);

  const applyE2ESession = useCallback(
    (session: E2ESession | null) => {
      if (!session) {
        clearE2ESession();
        setUser(null);
        setProfile(null);
        return;
      }

      writeE2ESession(session);
      setUser(toE2EUser(session));
      setProfile({
        uid: session.uid,
        id: session.uid,
        name: session.name,
        display_name: session.name,
        email: session.email,
        role: session.role,
      });
    },
    [toE2EUser]
  );

  const refreshProfile = useCallback(async () => {
    if (E2E_AUTH_BYPASS) {
      const existing = readE2ESession();
      applyE2ESession(existing);
      return;
    }

    if (!auth.currentUser) {
      setProfile(null);
      return;
    }
    try {
      const token = await auth.currentUser.getIdToken();
      const data = await apiFetch<UserProfile>("/api/users/me", { method: "GET" }, token);
      setProfile(data);
    } catch (err) {
      // Don't rethrow network errors — keep user signed in with a null profile
      // so the app can still render and retry later.
      console.warn("Could not load user profile (server may be starting):", (err as Error).message);
      setProfile(null);
    }
  }, [applyE2ESession]);

  useEffect(() => {
    if (E2E_AUTH_BYPASS) {
      queueMicrotask(() => {
        applyE2ESession(readE2ESession());
        setLoading(false);
      });
      return;
    }

    const unsubscribe = onAuthStateChanged(auth, async (firebaseUser) => {
      setUser(firebaseUser);
      if (firebaseUser) {
        await refreshProfile();
      } else {
        setProfile(null);
      }
      setLoading(false);
    });

    return () => unsubscribe();
  }, [applyE2ESession, refreshProfile]);

  const signIn = useCallback(async (email: string, password: string) => {
    if (E2E_AUTH_BYPASS) {
      const localPart = (email.split("@")[0] || "student").trim();
      applyE2ESession({
        uid: `e2e-${localPart}`,
        name: localPart || "Student",
        email,
        role: "viewer",
      });
      return;
    }

    await signInWithEmailAndPassword(auth, email, password);
    try {
      await refreshProfile();
    } catch (err) {
      console.error("Failed to load profile after sign-in", err);
    }
  }, [applyE2ESession, refreshProfile]);

  const signUp = useCallback(
    async (name: string, email: string, password: string) => {
      if (E2E_AUTH_BYPASS) {
        applyE2ESession({
          uid: `e2e-${Date.now()}`,
          name: name.trim() || "Student",
          email,
          role: "viewer",
        });
        return;
      }

      const credentials = await createUserWithEmailAndPassword(auth, email, password);
      await updateProfile(credentials.user, { displayName: name });
      const token = await credentials.user.getIdToken();
      try {
        await apiFetch<UserProfile>(
          "/api/users",
          {
            method: "POST",
            body: JSON.stringify({ name, role: "viewer" }),
          },
          token
        );
      } catch (err) {
        console.error("Failed to create user profile", err);
      }
      try {
        await refreshProfile();
      } catch (err) {
        console.error("Failed to load profile after sign-up", err);
      }
    },
    [applyE2ESession, refreshProfile]
  );

  const signInWithGoogle = useCallback(async () => {
    if (E2E_AUTH_BYPASS) {
      applyE2ESession({
        uid: `e2e-google-${Date.now()}`,
        name: "E2E Google User",
        email: "e2e.google@example.edu",
        role: "viewer",
      });
      return;
    }

    const provider = new GoogleAuthProvider();
    const credentials = await signInWithPopup(auth, provider);
    const token = await credentials.user.getIdToken();
    try {
      await apiFetch<UserProfile>(
        "/api/users",
        {
          method: "POST",
          body: JSON.stringify({
            name: credentials.user.displayName ?? "",
            role: "viewer",
          }),
        },
        token
      );
    } catch (err) {
      console.error("Failed to create/sync user profile", err);
    }
    try {
      await refreshProfile();
    } catch (err) {
      console.error("Failed to load profile after Google sign-in", err);
    }
  }, [applyE2ESession, refreshProfile]);

  const signOut = useCallback(async () => {
    if (E2E_AUTH_BYPASS) {
      applyE2ESession(null);
      return;
    }

    await firebaseSignOut(auth);
    setProfile(null);
  }, [applyE2ESession]);

  const value = useMemo(
    () => ({
      user,
      profile,
      loading,
      signIn,
      signUp,
      signInWithGoogle,
      signOut,
      refreshProfile,
    }),
    [user, profile, loading, signIn, signUp, signInWithGoogle, signOut, refreshProfile]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return context;
}
