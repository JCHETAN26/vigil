import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Verdict colors are always paired with a text label + icon/pattern (never color alone).
        regression: "#b42318",
        watch: "#b54708",
        ok: "#067647",
      },
    },
  },
  plugins: [],
};
export default config;
