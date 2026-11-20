/*
 * ESP32_Mesh_Hub.ino
 * USB Serial  <->  BLE Nordic UART Service (NUS) bridge
 *
 * Board   : ESP32 DevKit V1 (ESP-WROOM-32), select "ESP32 Dev Module"
 * Library : NimBLE-Arduino by h2zero, version 2.x  (Library Manager)
 *
 * Data flow (no echo, each direction is one-way):
 *   USB Serial line  --->  BLE TX notification  --->  phone
 *   phone BLE write  --->  USB Serial line      --->  laptop relay.py
 *
 * Lines the ESP32 prints about ITSELF start with "[ESP32]". relay.py
 * recognises that prefix and does NOT forward those lines to your friend.
 */

#include <Arduino.h>
#include <NimBLEDevice.h>

// ---------------- Configuration ----------------
static const char*    DEVICE_NAME = "ESP32_Mesh_Hub";
#define NUS_SERVICE_UUID "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
#define NUS_RX_UUID      "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  // phone writes here
#define NUS_TX_UUID      "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  // ESP32 notifies here

static const uint32_t USB_BAUD          = 115200;  // must match relay.py ESP_BAUD
   // longest message handled as one line
static const uint16_t DEFAULT_CHUNK     = 20;      // safe notify size when MTU is 23
static const uint32_t BLE_IDLE_FLUSH_MS = 300;     // send phone text even without newline
// -----------------------------------------------

NimBLEServer*         server = nullptr;
NimBLECharacteristic* txChar = nullptr;

volatile uint16_t notifyChunk = DEFAULT_CHUNK;  // updated when phone negotiates larger MTU
volatile bool evtConnected    = false;          // set in BLE callbacks, printed from loop()
volatile bool evtDisconnected = false;

// ---- Small ring buffer: BLE callback (producer) -> loop() (consumer) ----
static const size_t RING_SIZE = 2048;
static uint8_t  ring[RING_SIZE];
static size_t   ringHead = 0, ringTail = 0;
static portMUX_TYPE ringMux = portMUX_INITIALIZER_UNLOCKED;

static void ringPush(const uint8_t* data, size_t len) {
  portENTER_CRITICAL(&ringMux);
  for (size_t i = 0; i < len; i++) {
    size_t next = (ringHead + 1) % RING_SIZE;
    if (next == ringTail) break;  // full: drop the rest
    ring[ringHead] = data[i];
    ringHead = next;
  }
  portEXIT_CRITICAL(&ringMux);
}

static int ringPop() {
  int v = -1;
  portENTER_CRITICAL(&ringMux);
  if (ringTail != ringHead) {
    v = ring[ringTail];
    ringTail = (ringTail + 1) % RING_SIZE;
  }
  portEXIT_CRITICAL(&ringMux);
  return v;
}

// ---- BLE callbacks (NimBLE-Arduino 2.x signatures) ----
class ServerCallbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer* s, NimBLEConnInfo& connInfo) override {
    notifyChunk = DEFAULT_CHUNK;
    evtConnected = true;
  }
  void onDisconnect(NimBLEServer* s, NimBLEConnInfo& connInfo, int reason) override {
    notifyChunk = DEFAULT_CHUNK;
    evtDisconnected = true;
    NimBLEDevice::getAdvertising()->start();  // become visible again
  }
  void onMTUChange(uint16_t MTU, NimBLEConnInfo& connInfo) override {
    uint16_t payload = (MTU > 3) ? (MTU - 3) : DEFAULT_CHUNK;  // ATT header = 3 bytes
    if (payload < DEFAULT_CHUNK) payload = DEFAULT_CHUNK;
    if (payload > 244) payload = 244;
    notifyChunk = payload;
  }
};

class RxCallbacks : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* ch, NimBLEConnInfo& connInfo) override {
    auto v = ch->getValue();
    ringPush((const uint8_t*)v.data(), v.length());
  }
};

// ---- Phone -> USB Serial ----
static char   bleLine[LINE_MAX];
static size_t bleLen = 0;
static uint32_t bleLastByteMs = 0;

static void flushBleLine() {
  if (bleLen == 0) return;
  Serial.write((const uint8_t*)bleLine, bleLen);
  Serial.write('\n');  // always exactly one '\n', whatever the phone app sent
  bleLen = 0;
}

static void pumpBleToUsb() {
  int b;
  while ((b = ringPop()) >= 0) {
    bleLastByteMs = millis();
    char c = (char)b;
    if (c == '\n' || c == '\r') {        // handles \n, \r and \r\n (empty lines are skipped)
      flushBleLine();
    } else {
      if (bleLen >= LINE_MAX - 1) flushBleLine();
      bleLine[bleLen++] = c;
    }
  }
  // Many phone apps send text with no newline: flush after a short idle time.
  if (bleLen > 0 && (millis() - bleLastByteMs) > BLE_IDLE_FLUSH_MS) flushBleLine();
}

// ---- USB Serial -> Phone ----
static char   usbLine[LINE_MAX + 2];
static size_t usbLen = 0;

static void sendUsbLineToBle() {
  if (usbLen == 0) return;
  if (server->getConnectedCount() == 0) {
    Serial.println("[ESP32] No BLE client connected - message dropped");
    usbLen = 0;
    return;
  }
  usbLine[usbLen++] = '\n';  // so the phone terminal shows one line per message
  size_t off = 0;
  while (off < usbLen) {
    size_t n = usbLen - off;
    if (n > notifyChunk) n = notifyChunk;
    // Do not cut a UTF-8 multi-byte character in half
    if (off + n < usbLen) {
      while (n > 1 && (((uint8_t)usbLine[off + n]) & 0xC0) == 0x80) n--;
    }
    txChar->setValue((const uint8_t*)(usbLine + off), n);
    txChar->notify();
    off += n;
    delay(8);  // let the BLE stack drain; avoids dropped notifications
  }
  usbLen = 0;
}

static void pumpUsbToBle() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      sendUsbLineToBle();
    } else {
      if (usbLen >= LINE_MAX) sendUsbLineToBle();
      usbLine[usbLen++] = c;
    }
  }
}

static void printEvents() {
  if (evtConnected)    { evtConnected = false;    Serial.println("[ESP32] BLE client connected"); }
  if (evtDisconnected) { evtDisconnected = false; Serial.println("[ESP32] BLE client disconnected"); }
}

void setup() {
  Serial.setRxBufferSize(2048);
  Serial.begin(USB_BAUD);
  delay(300);

  NimBLEDevice::init(DEVICE_NAME);
  NimBLEDevice::setMTU(247);

  server = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());

  NimBLEService* svc = server->createService(NUS_SERVICE_UUID);

  NimBLECharacteristic* rx = svc->createCharacteristic(
      NUS_RX_UUID, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
  rx->setCallbacks(new RxCallbacks());

  txChar = svc->createCharacteristic(NUS_TX_UUID, NIMBLE_PROPERTY::NOTIFY);

  server->start();

  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->setName(DEVICE_NAME);
  adv->addServiceUUID(NUS_SERVICE_UUID);
  adv->enableScanResponse(true);
  adv->start();

  Serial.println("[ESP32] Ready. Advertising as ESP32_Mesh_Hub");
}

void loop() {
  printEvents();
  pumpBleToUsb();
  pumpUsbToBle();
  delay(1);
}
