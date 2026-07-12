import { Link } from "@tanstack/react-router";

export function NotFound() {
  return (
    <main style={{ maxWidth: "36rem", margin: "4rem auto", padding: "0 1.5rem" }}>
      <h1>Page not found</h1>
      <p>
        This page does not exist or may have moved. Check the address, or return home to find what
        you need.
      </p>
      <p>
        <Link to="/">Go to home</Link>
      </p>
    </main>
  );
}
