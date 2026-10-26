FROM python:3.12-alpine

RUN apk add --no-cache git jq bash ffmpeg

ARG BUILD_VERSION
RUN echo "Building bridge source for add-on version ${BUILD_VERSION}" \
	&& git clone --depth=1 https://github.com/ViperRNMC/anycubic-s1-mqtt-bridge.git /anycubic

WORKDIR /anycubic
RUN pip install --no-cache-dir -r requirements.txt

COPY run.sh /run.sh
RUN sed -i 's/\r$//' /run.sh && chmod +x /run.sh

CMD ["bash", "/run.sh"]
