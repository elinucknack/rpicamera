import base64
import http.server
import json
import logging
import logging.handlers
import io
import os
import paho.mqtt.client
import picamera2
import picamera2.encoders
import picamera2.outputs
import socketserver
import threading
import time

def create_rpi_camera_logger(name):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    
    syslog_handler = logging.handlers.SysLogHandler(address='/dev/log')
    syslog_handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
    logger.addHandler(syslog_handler)
    
    file_handler = logging.FileHandler(os.path.dirname(__file__) + '/app.log')
    file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(file_handler)
    
    return logger;

class RPiCameraState:
    def __init__(self, logger):
        self.logger = logger

    def read(self):
        default_state = {'on': False}
        if not os.path.exists(os.path.dirname(__file__) + '/state.json'):
            self.logger.info(f'State file does not exist. New state file created.')
            self.write(default_state)
            return default_state
        try:
            with open(os.path.dirname(__file__) + '/state.json', 'r') as file:
                return json.load(file)
        except Exception as e:
            self.logger.error(f'Invalid state file content. New state file created.')
            self.write(default_state)
            return default_state

    def write(self, state):
        try:
            with open(os.path.dirname(__file__) + '/state.json', 'w') as file:
                json.dump(state, file)
        except Exception as e:
            self.logger.error(e)

class RPiCameraOutput(io.BufferedIOBase):
    def __init__(self):
        super().__init__()
        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()

class RPiCamera(picamera2.Picamera2):
    def __init__(self, frame_width, frame_height, frame_rate, logger):
        super().__init__()
        self.configure(self.create_video_configuration(main={"size": (frame_width, frame_height)}, controls={"FrameRate": frame_rate}))
        self.state = RPiCameraState(logger)
        self.output = RPiCameraOutput()
        self.logger = logger
        if self.state.read()['on']:
            self.start_recording()
        else:
            self.stream_on = False

    def start_recording(self):
        super().start_recording(picamera2.encoders.JpegEncoder(), picamera2.outputs.FileOutput(self.output))
        self.stream_on = True
        self.logger.info('Stream started')

    def stop_recording(self):
        super().stop_recording()
        self.stream_on = False
        self.logger.info('Stream stopped')

    def write_state(self):
        self.state.write({'on': self.stream_on})

class RPiCameraStreamHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/stream':
            self.send_response(200)
            self.send_header('Age', 0)
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=FRAME')
            self.end_headers()
            try:
                while True:
                    with self.server.rpi_camera.output.condition:
                        self.server.rpi_camera.output.condition.wait()
                        frame = self.server.rpi_camera.output.frame
                    self.wfile.write(b'--FRAME\r\n')
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', len(frame))
                    self.end_headers()
                    self.wfile.write(frame)
                    self.wfile.write(b'\r\n')
            except Exception as e:
                self.server.logger.warning('Client %s removed: %s', self.client_address, str(e))
        else:
            self.send_error(404)
            self.end_headers()

class RPiCameraStreamServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    
    def __init__(self, server_address, handler_class, rpi_camera, logger):
        super().__init__(server_address, handler_class)
        self.rpi_camera = rpi_camera
        self.logger = logger

class MqttClient(paho.mqtt.client.Client):
    def __init__(self, topic, rpi_camera, logger):
        super().__init__()
        self.topic = topic
        self.rpi_camera = rpi_camera
        self.logger = logger
    
    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.logger.info('Connected to broker')
            self.subscribe(self.topic + '/#', qos=2)
        else:
            self.logger.info(f'Connection attempt failed with error code {rc}')  
    
    def on_disconnect(self, client, userdata, rc):
        self.logger.info('Disconnected from broker. Next connection attempt in 5 seconds...')
        time.sleep(5)
        self.start_reconnection()
    
    def on_message(self, client, userdata, msg):
        if msg.topic == self.topic + '/on' and not self.rpi_camera.stream_on:
            self.rpi_camera.start_recording()
            self.rpi_camera.write_state()
            self.send_state()
        if msg.topic == self.topic + '/off' and self.rpi_camera.stream_on:
            self.rpi_camera.stop_recording()
            self.rpi_camera.write_state()
            self.send_state()
    
    def start_connection(self, host, port):
        try:
            self.connect(host, port, 60)
        except Exception as e:
            self.logger.info(f'First connection attempt failed: {e}. Next connection attempt in 5 seconds...')
            time.sleep(5)
            self.start_reconnection()
    
    def start_reconnection(self):
        while True:
            try:
                self.reconnect()
                self.logger.info('Re-connected to broker')
                break
            except Exception as e:
                self.logger.info(f'Re-connection attempt failed. Next connection attempt in 5 seconds...')
                time.sleep(5)
    
    def send_state(self):
        self.publish(self.topic + '/state', json.dumps({'timestamp': int(time.time()), 'on': rpi_camera.stream_on}), qos=2, retain=True)
    
    def set_ssl_certificates(self, ca, cert, key):
        self.tls_set(ca_certs=os.path.dirname(__file__) + '/' + ca, certfile=os.path.dirname(__file__) + '/' + cert, keyfile=os.path.dirname(__file__) + '/' + key)
    
    def set_credentials(self, username, pw):
        self.username_pw_set(username, pw)

logger = create_rpi_camera_logger(os.getenv('APP_LOGGER_NAME'))

rpi_camera = RPiCamera(int(os.getenv('APP_STREAM_FRAME_WIDTH')), int(os.getenv('APP_STREAM_FRAME_HEIGHT')), int(os.getenv('APP_STREAM_FRAME_RATE')), logger)

stream_server = RPiCameraStreamServer(('', int(os.getenv('APP_STREAM_PORT'))), RPiCameraStreamHandler, rpi_camera, logger)

mqtt_client = MqttClient(os.getenv('APP_MQTT_TOPIC'), rpi_camera, logger)
if os.getenv('APP_MQTT_USE_SECURE_CONNECTION') == 'true':
    mqtt_client.set_ssl_certificates(os.getenv('APP_MQTT_CA_FILENAME'), os.getenv('APP_MQTT_CERT_FILENAME'), os.getenv('APP_MQTT_KEY_FILENAME'))
mqtt_client.set_credentials(os.getenv('APP_MQTT_USERNAME'), base64.b64decode(os.getenv('APP_MQTT_PASSWORD')))

def stream_server_thread():
    stream_server.serve_forever()

threading.Thread(target=stream_server_thread, daemon=True).start()

def send_state_thread():
    while True:
        mqtt_client.send_state()
        time.sleep(15)

threading.Thread(target=send_state_thread, daemon=True).start()

mqtt_client.start_connection(os.getenv('APP_MQTT_HOST'), int(os.getenv('APP_MQTT_PORT')))
mqtt_client.loop_forever()
