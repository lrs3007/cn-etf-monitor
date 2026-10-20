import type { NextConfig } from "next";

const published = process.env.NEXT_PUBLIC_DATA_MODE === "published";
const config: NextConfig = {
  output: published ? "export" : process.env.STANDALONE_BUILD === "1" ? "standalone" : undefined,
  // Next's static export uses .next internally even with a custom export path.
  // Keep the running local service outside that directory.
  distDir: published ? ".next-published" : process.env.STANDALONE_BUILD === "1" ? ".next" : ".next-local",
  ...(published ? {} : { async rewrites() {
    return [{ source: "/api/:path*", destination: `${process.env.API_BASE_URL || "http://127.0.0.1:8000"}/api/:path*` }];
  } }),
};
export default config;
