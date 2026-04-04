"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { useAuth } from "@/context/AuthContext";
import { AccessDenied, LoadingState } from "@/components/States";

export function ProtectedRoute({
  children,
  requiredRole,
}: {
  children: React.ReactNode;
  requiredRole?: "viewer" | "contributor" | "placement_cell";
}) {
  const router = useRouter();
  const { user, loading, profile } = useAuth();

  useEffect(() => {
    if (!loading && !user) {
      router.replace("/login");
    }
  }, [loading, router, user]);

  if (loading) {
    return <LoadingState message="Checking your access..." />;
  }

  if (!user) {
    return null;
  }

  if (requiredRole) {
    const rolePriority = {
      viewer: 0,
      contributor: 1,
      placement_cell: 2,
    } as const;
    const currentRole = profile?.role ?? "viewer";
    if (rolePriority[currentRole] < rolePriority[requiredRole]) {
      return <AccessDenied requiredRole={requiredRole} />;
    }
  }

  return <>{children}</>;
}
