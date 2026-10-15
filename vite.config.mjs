import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
export default defineConfig({
  root: "frontend",
  base: "./",
  plugins: [vue()],
  build: { outDir: "../build/ui", emptyOutDir: true, sourcemap: false },
});
