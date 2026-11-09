FROM node:20-alpine as build
WORKDIR /app

COPY academic-kb-prototype-MVP/frontend/package*.json ./
RUN npm install

COPY academic-kb-prototype-MVP/frontend/ ./
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80