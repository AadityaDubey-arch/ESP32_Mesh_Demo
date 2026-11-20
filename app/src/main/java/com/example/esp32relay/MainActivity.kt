
package com.example.esp32relay

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothServerSocket
import android.bluetooth.BluetoothSocket
import android.bluetooth.BluetoothStatusCodes
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.widget.Button
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStream
import java.nio.charset.StandardCharsets
import java.util.UUID
import java.util.concurrent.ConcurrentLinkedQueue

class MainActivity : AppCompatActivity() {

    companion object {
        private const val DEVICE_NAME = "ESP32_Mesh_Hub"

        private val SPP_UUID: UUID =
            UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")

        private val UART_SERVICE_UUID: UUID =
            UUID.fromString("6E400001-B5A3-F393-E0A9-E50E24DCCA9E")

        private val UART_RX_UUID: UUID =
            UUID.fromString("6E400002-B5A3-F393-E0A9-E50E24DCCA9E")

        private val UART_TX_UUID: UUID =
            UUID.fromString("6E400003-B5A3-F393-E0A9-E50E24DCCA9E")

        private val CCCD_UUID: UUID =
            UUID.fromString("00002902-0000-1000-8000-00805F9B34FB")
    }

    private lateinit var statusText: TextView
    private lateinit var logText: TextView

    private val bluetoothAdapter: BluetoothAdapter? by lazy {
        val manager = getSystemService(BLUETOOTH_SERVICE) as BluetoothManager
        manager.adapter
    }

    @Volatile private var serverSocket: BluetoothServerSocket? = null
    @Volatile private var classicSocket: BluetoothSocket? = null
    @Volatile private var classicOutput: OutputStream? = null
    @Volatile private var bleGatt: BluetoothGatt? = null

    @Volatile private var rxCharacteristic: BluetoothGattCharacteristic? = null
    @Volatile private var bleReady = false
    @Volatile private var stopping = false
    @Volatile private var bleWriteInProgress = false

    private val bleWriteQueue = ConcurrentLinkedQueue<ByteArray>()

    private var pendingAction: (() -> Unit)? = null

    private val permissionLauncher =
        registerForActivityResult(
            ActivityResultContracts.RequestMultiplePermissions()
        ) { results ->
            val granted = results.values.all { it }

            if (granted) {
                pendingAction?.invoke()
            } else {
                log("Bluetooth permissions were denied.")
            }

            pendingAction = null
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        statusText = findViewById(R.id.statusText)
        logText = findViewById(R.id.logText)

        findViewById<Button>(R.id.startClassicButton).setOnClickListener {
            withBluetoothPermissions { startClassicServer() }
        }

        findViewById<Button>(R.id.connectBleButton).setOnClickListener {
            withBluetoothPermissions { scanForEsp32() }
        }

        findViewById<Button>(R.id.stopButton).setOnClickListener {
            stopRelay()
        }

        log("ESP32Relay ready.")
        log("Start the laptop server, then connect to the ESP32.")
    }

    private fun requiredPermissions(): Array<String> {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            arrayOf(
                Manifest.permission.BLUETOOTH_CONNECT,
                Manifest.permission.BLUETOOTH_SCAN,
                Manifest.permission.BLUETOOTH_ADVERTISE
            )
        } else {
            arrayOf(Manifest.permission.ACCESS_FINE_LOCATION)
        }
    }

    private fun withBluetoothPermissions(action: () -> Unit) {
        val missing = requiredPermissions().filter {
            checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED
        }

        if (missing.isEmpty()) {
            action()
        } else {
            pendingAction = action
            permissionLauncher.launch(missing.toTypedArray())
        }
    }

    @SuppressLint("MissingPermission")
    private fun startClassicServer() {
        val adapter = bluetoothAdapter

        if (adapter == null) {
            log("This phone does not support Bluetooth.")
            return
        }

        if (!adapter.isEnabled) {
            log("Turn on Bluetooth in Android Settings, then try again.")
            return
        }

        if (serverSocket != null) {
            log("Classic Bluetooth server is already running.")
            return
        }

        stopping = false

        try {
            // Ask Android to make the phone discoverable.
            val discoverable = Intent(
                BluetoothAdapter.ACTION_REQUEST_DISCOVERABLE
            ).apply {
                putExtra(
                    BluetoothAdapter.EXTRA_DISCOVERABLE_DURATION,
                    300
                )
            }
            startActivity(discoverable)

            Thread {
                try {
                    val server = adapter.listenUsingRfcommWithServiceRecord(
                        "ESP32Relay",
                        SPP_UUID
                    )

                    serverSocket = server

                    log("Waiting for a laptop over Bluetooth Classic...")

                    val socket = server.accept()

                    if (stopping) {
                        socket.close()
                        return@Thread
                    }

                    classicSocket = socket
                    classicOutput = socket.outputStream

                    log("Laptop connected: ${socket.remoteDevice.name}")
                    updateStatus("Laptop connected; waiting for BLE")

                    readClassicMessages(socket)
                } catch (e: Exception) {
                    if (!stopping) {
                        log("Classic Bluetooth error: ${e.message}")
                    }
                } finally {
                    try {
                        serverSocket?.close()
                    } catch (_: Exception) {
                    }
                    serverSocket = null
                }
            }.start()

        } catch (e: Exception) {
            log("Could not start Bluetooth server: ${e.message}")
        }
    }

    @SuppressLint("MissingPermission")
    private fun readClassicMessages(socket: BluetoothSocket) {
        try {
            val reader = BufferedReader(
                InputStreamReader(
                    socket.inputStream,
                    StandardCharsets.UTF_8
                )
            )

            while (!stopping) {
                val line = reader.readLine() ?: break

                log("Laptop → ESP32: $line")
                sendToBle((line + "\n").toByteArray(StandardCharsets.UTF_8))
            }

            log("Laptop Bluetooth connection closed.")
        } catch (e: Exception) {
            if (!stopping) {
                log("Laptop read error: ${e.message}")
            }
        }
    }

    @SuppressLint("MissingPermission")
    private fun scanForEsp32() {
        val adapter = bluetoothAdapter

        if (adapter == null || !adapter.isEnabled) {
            log("Turn on Bluetooth first.")
            return
        }

        val scanner = adapter.bluetoothLeScanner

        if (scanner == null) {
            log("BLE scanning is unavailable.")
            return
        }

        stopping = false
        log("Scanning for $DEVICE_NAME...")

        try {
            scanner.startScan(
                null,
                ScanSettings.Builder()
                    .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
                    .build(),
                bleScanCallback
            )
        } catch (e: Exception) {
            log("BLE scan error: ${e.message}")
        }
    }

    private val bleScanCallback = object : ScanCallback() {
        @SuppressLint("MissingPermission")
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val advertisedName =
                result.scanRecord?.deviceName
                    ?: try {
                        result.device.name
                    } catch (_: SecurityException) {
                        null
                    }

            if (advertisedName == DEVICE_NAME && bleGatt == null) {
                try {
                    bluetoothAdapter?.bluetoothLeScanner
                        ?.stopScan(this)
                } catch (_: Exception) {
                }

                log("ESP32 found. Connecting...")
                updateStatus("Connecting to ESP32")

                runOnUiThread {
                    connectBle(result.device)
                }
            }
        }

        override fun onScanFailed(errorCode: Int) {
            log("BLE scan failed: code $errorCode")
        }
    }

    @SuppressLint("MissingPermission")
    private fun connectBle(device: BluetoothDevice) {
        if (bleGatt != null) {
            log("BLE connection already exists.")
            return
        }

        try {
            bleGatt = device.connectGatt(
                this,
                false,
                gattCallback,
                BluetoothDevice.TRANSPORT_LE
            )
        } catch (e: Exception) {
            log("BLE connection error: ${e.message}")
        }
    }

    private val gattCallback = object : BluetoothGattCallback() {

        @SuppressLint("MissingPermission")
        override fun onConnectionStateChange(
            gatt: BluetoothGatt,
            status: Int,
            newState: Int
        ) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                log("BLE connection failed: status $status")
                closeGatt(gatt)
                return
            }

            when (newState) {
                BluetoothProfile.STATE_CONNECTED -> {
                    log("Connected to ESP32. Discovering services...")
                    gatt.discoverServices()
                }

                BluetoothProfile.STATE_DISCONNECTED -> {
                    bleReady = false
                    rxCharacteristic = null
                    log("ESP32 BLE disconnected.")
                    updateStatus("ESP32 disconnected")
                    closeGatt(gatt)
                }
            }
        }

        @SuppressLint("MissingPermission")
        override fun onServicesDiscovered(
            gatt: BluetoothGatt,
            status: Int
        ) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                log("Could not discover BLE services.")
                return
            }

            val service = gatt.getService(UART_SERVICE_UUID)

            if (service == null) {
                log("UART service not found. Check ESP32 firmware UUIDs.")
                return
            }

            val rx = service.getCharacteristic(UART_RX_UUID)
            val tx = service.getCharacteristic(UART_TX_UUID)

            if (rx == null || tx == null) {
                log("UART RX/TX characteristic missing.")
                return
            }

            rxCharacteristic = rx

            val notificationEnabled =
                gatt.setCharacteristicNotification(tx, true)

            val descriptor = tx.getDescriptor(CCCD_UUID)

            if (!notificationEnabled || descriptor == null) {
                log("Could not enable BLE notifications.")
                return
            }

            val writeStarted =
                if (Build.VERSION.SDK_INT >= 33) {
                    gatt.writeDescriptor(
                        descriptor,
                        BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                    ) == BluetoothStatusCodes.SUCCESS
                } else {
                    @Suppress("DEPRECATION")
                    run {
                        descriptor.value =
                            BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                        gatt.writeDescriptor(descriptor)
                    }
                }

            if (!writeStarted) {
                log("Could not subscribe to ESP32 notifications.")
            }
        }

        @SuppressLint("MissingPermission")
        override fun onDescriptorWrite(
            gatt: BluetoothGatt,
            descriptor: BluetoothGattDescriptor,
            status: Int
        ) {
            if (descriptor.uuid == CCCD_UUID &&
                status == BluetoothGatt.GATT_SUCCESS
            ) {
                bleReady = true
                log("ESP32 BLE UART ready.")
                updateStatus("BLE connected; relay ready")
                writeNextBle()
            } else {
                log("Notification subscription failed: $status")
            }
        }

        @Deprecated("Legacy Android BLE callback")
        @SuppressLint("MissingPermission")
        override fun onCharacteristicChanged(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic
        ) {
            @Suppress("DEPRECATION")
            val data = characteristic.value ?: return
            handleBleNotification(characteristic, data)
        }

        @SuppressLint("MissingPermission")
        override fun onCharacteristicChanged(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            value: ByteArray
        ) {
            handleBleNotification(characteristic, value)
        }

        @SuppressLint("MissingPermission")
        override fun onCharacteristicWrite(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            status: Int
        ) {
            synchronized(bleWriteQueue) {
                bleWriteInProgress = false
            }

            if (status != BluetoothGatt.GATT_SUCCESS) {
                log("BLE message write failed: $status")
            }

            writeNextBle()
        }
    }

    private fun handleBleNotification(
        characteristic: BluetoothGattCharacteristic,
        data: ByteArray
    ) {
        if (characteristic.uuid != UART_TX_UUID) return

        val message = String(data, StandardCharsets.UTF_8)
        log("ESP32 → laptop: ${message.trimEnd()}")

        try {
            val output = classicOutput

            if (output == null) {
                log("No laptop is connected to Bluetooth Classic.")
                return
            }

            output.write(data)
            output.flush()
        } catch (e: Exception) {
            log("Could not forward to laptop: ${e.message}")
        }
    }

    private fun sendToBle(data: ByteArray) {
        if (!bleReady || rxCharacteristic == null) {
            log("BLE UART is not ready; message was not forwarded.")
            return
        }

        synchronized(bleWriteQueue) {
            bleWriteQueue.add(data)
        }

        writeNextBle()
    }

    @SuppressLint("MissingPermission")
    private fun writeNextBle() {
        val gatt = bleGatt ?: return
        val characteristic = rxCharacteristic ?: return

        if (!bleReady) return

        val next: ByteArray

        synchronized(bleWriteQueue) {
            if (bleWriteInProgress) return

            next = bleWriteQueue.poll() ?: return
            bleWriteInProgress = true
        }

        val started = try {
            if (Build.VERSION.SDK_INT >= 33) {
                gatt.writeCharacteristic(
                    characteristic,
                    next,
                    BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                ) == BluetoothStatusCodes.SUCCESS
            } else {
                @Suppress("DEPRECATION")
                run {
                    characteristic.writeType =
                        BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                    characteristic.value = next
                    gatt.writeCharacteristic(characteristic)
                }
            }
        } catch (e: Exception) {
            log("BLE write error: ${e.message}")
            false
        }

        if (!started) {
            synchronized(bleWriteQueue) {
                bleWriteInProgress = false
            }
            log("Could not send message to ESP32.")
        }
    }

    private fun updateStatus(message: String) {
        runOnUiThread {
            statusText.text = "Status: $message"
        }
    }

    private fun log(message: String) {
        runOnUiThread {
            logText.append("\n$message")
        }
    }

    @SuppressLint("MissingPermission")
    private fun stopRelay() {
        stopping = true
        bleReady = false
        rxCharacteristic = null

        try {
            bluetoothAdapter?.bluetoothLeScanner
                ?.stopScan(bleScanCallback)
        } catch (_: Exception) {
        }

        try {
            serverSocket?.close()
        } catch (_: Exception) {
        }

        try {
            classicSocket?.close()
        } catch (_: Exception) {
        }

        try {
            bleGatt?.disconnect()
            bleGatt?.close()
        } catch (_: Exception) {
        }

        serverSocket = null
        classicSocket = null
        classicOutput = null
        bleGatt = null

        synchronized(bleWriteQueue) {
            bleWriteQueue.clear()
            bleWriteInProgress = false
        }

        updateStatus("Stopped")
        log("Relay stopped.")
    }

    @SuppressLint("MissingPermission")
    private fun closeGatt(gatt: BluetoothGatt) {
        if (bleGatt === gatt) {
            bleGatt = null
            bleReady = false
            rxCharacteristic = null
        }

        try {
            gatt.close()
        } catch (_: Exception) {
        }
    }

    override fun onDestroy() {
        stopRelay()
        super.onDestroy()
    }
}
