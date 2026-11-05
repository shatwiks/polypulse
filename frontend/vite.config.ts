import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
const target = process.env.POLYPULSE_API_TARGET || 'http://localhost:8000'
export default defineConfig({plugins:[react()],server:{proxy:{'/api':target,'/ws':{target:target.replace(/^http/,'ws'),ws:true}}}})
