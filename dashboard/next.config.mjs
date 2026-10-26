/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The mocked Playwright server builds into its own dir so it never clobbers `.next`.
  distDir: process.env.NEXT_DIST_DIR ?? ".next",
};
export default nextConfig;
