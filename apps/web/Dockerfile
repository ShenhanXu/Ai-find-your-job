FROM node:22-alpine

WORKDIR /app

ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

COPY package.json .
RUN npm install

COPY . .

EXPOSE 3000

RUN npm run build

CMD ["npm", "start"]
