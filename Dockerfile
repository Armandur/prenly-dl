FROM alpine:3.14
WORKDIR /app

RUN apk add curl
RUN apk add unzip

# Install python/pip

ENV PYTHONUNBUFFERED=1
RUN apk add --update python3 && ln -sf python3 /usr/bin/python

RUN python3 -m ensurepip
RUN pip3 install --upgrade pip setuptools

# Install reqs
# TODO Why won't pipenv install work?
RUN apk add py3-pikepdf
RUN pip install requests pypdf2 img2pdf

# Download and unzip
RUN curl -L https://github.com/Armandur/prenly-dl/archive/refs/heads/main.zip -o "main.zip" && unzip "main.zip"
RUN mv prenly-dl-main/* . && rm -rf prenly-dl-main/ && rm main.zip

# Make configuration and output directory
RUN mkdir /conf
RUN mkdir /output
RUN mv *.json /conf/

ENV CONF_FILE="default.json"

WORKDIR /output

#Create startup-script
RUN echo "python /app/prenly-dl.py --json=/conf/\$CONF_FILE" > /app/startup.sh

ENTRYPOINT ["/bin/sh", "/app/startup.sh"]