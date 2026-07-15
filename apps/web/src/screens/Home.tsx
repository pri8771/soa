import { Link } from "@tanstack/react-router";

import { useAuth } from "../auth/AuthContext";

export function Home() {
  const auth = useAuth();
  return (
    <main style={{ maxWidth: "36rem", margin: "4rem auto", padding: "0 1.5rem" }}>
      <h1>SOA</h1>
      <p>Turn incoming sales documents into reviewed, reliable orders.</p>
      <p>
        {auth.isAuthenticated ? (
          <Link to="/select-organization">Open your organizations</Link>
        ) : (
          <Link to="/login">Sign in with your organization</Link>
        )}
      </p>
    </main>
  );
}
