/* GitHub Pages 部署配置
 * __API_BASE__ : 后端公网地址（cloudflared 隧道 URL，重启隧道后地址会变，需同步更新）
 * __API_TOKEN__: 与后端 .env 中 API_TOKEN 一致，保护后端不被滥用
 */
window.__API_BASE__ = 'https://paragraphs-administrators-weekly-bridal.trycloudflare.com';
window.__API_TOKEN__ = 'rsm-9f2c4e7a1b8d3f60';