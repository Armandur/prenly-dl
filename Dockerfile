FROM alpine:3.14
WORKDIR /app

RUN apk add curl
RUN apk add unzip

# Install python/pip
ENV PYTHONUNBUFFERED=1
RUN apk add --update python3 && ln -sf python3 /usr/bin/python
RUN apk add py3-pdf2
RUN apk add py3-img2pdf
RUN apk add py3-requests

RUN curl -L https://github.com/Armandur/prenly-dl/archive/refs/heads/main.zip -o "main.zip" && unzip "main.zip"
RUN mv prenly-dl-main/* . && rm -rf prenly-dl-main/ && rm main.zip

RUN /bin/sh