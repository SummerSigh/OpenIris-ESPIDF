/*
 * SPDX-FileCopyrightText: 2022-2024 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include <string.h>
#include <inttypes.h>
#include <stdint.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_timer.h"
#include "esp_check.h"
#if CONFIG_TINYUSB_RHPORT_HS
#include "soc/hp_sys_clkrst_reg.h"
#include "soc/hp_system_reg.h"
#else
#include "esp_private/usb_phy.h"
#endif
#include "tusb.h"
#include "usb_device_uvc.h"

static const char *TAG = "usbd_uvc";

#if CONFIG_UVC_SUPPORT_TWO_CAM
#define UVC_CAM_NUM 2
#else
#define UVC_CAM_NUM 1
#endif

typedef struct
{
#if !CONFIG_TINYUSB_RHPORT_HS
    usb_phy_handle_t phy_hdl;
#endif
    bool uvc_init[UVC_CAM_NUM];
    uvc_format_t format[UVC_CAM_NUM];
    uvc_device_config_t user_config[UVC_CAM_NUM];
    TaskHandle_t uvc_task_hdl[UVC_CAM_NUM];
    uint32_t interval_ms[UVC_CAM_NUM];
} uvc_device_t;

static uvc_device_t s_uvc_device;

static void usb_phy_init(void)
{
#if !CONFIG_TINYUSB_RHPORT_HS
    // Configure USB PHY
    usb_phy_config_t phy_conf = {
        .controller = USB_PHY_CTRL_OTG,
        .otg_mode = USB_OTG_MODE_DEVICE,
        .target = USB_PHY_TARGET_INT,
    };
    usb_new_phy(&phy_conf, &s_uvc_device.phy_hdl);
#endif
}

static inline uint32_t get_time_millis(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static void tusb_device_task(void *arg)
{
    while (1)
    {
        tud_task();
    }
}

void tud_mount_cb(void)
{
    ESP_LOGI(TAG, "Mount");
}

// Invoked when device is unmounted
void tud_umount_cb(void)
{
    ESP_LOGI(TAG, "UN-Mount");
}

void tud_suspend_cb(bool remote_wakeup_en)
{
    (void)remote_wakeup_en;

    if (s_uvc_device.user_config[0].stop_cb)
    {
        s_uvc_device.user_config[0].stop_cb(s_uvc_device.user_config[0].cb_ctx);
    }
#if CONFIG_UVC_SUPPORT_TWO_CAM
    if (s_uvc_device.user_config[1].stop_cb)
    {
        s_uvc_device.user_config[1].stop_cb(s_uvc_device.user_config[1].cb_ctx);
    }
#endif
    ESP_LOGI(TAG, "Suspend");
}

// Invoked when usb bus is resumed
void tud_resume_cb(void)
{
    ESP_LOGI(TAG, "Resume");
}

//--------------------------------------------------------------------+
// USB CDC
//--------------------------------------------------------------------+
#if (CFG_TUD_CDC)
// External callback to handle CDC data
extern void uvc_cdc_rx_callback(const uint8_t* buffer, size_t length);

static char cdc_rx_buffer[512] = {0};
static size_t cdc_rx_pos = 0;

void tud_cdc_rx_cb(uint8_t itf)
{
    (void) itf;
    
    uint8_t buf[64];
    uint32_t count = tud_cdc_available();
    if (count > 0) 
    {
        uint32_t bytes_read = tud_cdc_read(buf, sizeof(buf));
        if (bytes_read > 0)
        {
            // Accumulate data in buffer
            for (uint32_t i = 0; i < bytes_read && cdc_rx_pos < sizeof(cdc_rx_buffer) - 1; i++)
            {
                cdc_rx_buffer[cdc_rx_pos++] = buf[i];
                
                // Check for complete command (newline or end of JSON)
                if (buf[i] == '\n' || buf[i] == '\r')
                {
                    cdc_rx_buffer[cdc_rx_pos - 1] = '\0'; // Replace newline with null terminator
                    
                    // Process complete command
                    if (cdc_rx_pos > 1)
                    {
                        uvc_cdc_rx_callback((const uint8_t*)cdc_rx_buffer, cdc_rx_pos - 1);
                    }
                    
                    // Reset buffer
                    cdc_rx_pos = 0;
                    cdc_rx_buffer[0] = '\0';
                    return;
                }
            }
            
            // If buffer is getting full without newline, process anyway
            if (cdc_rx_pos >= sizeof(cdc_rx_buffer) - 1)
            {
                cdc_rx_buffer[cdc_rx_pos] = '\0';
                uvc_cdc_rx_callback((const uint8_t*)cdc_rx_buffer, cdc_rx_pos);
                cdc_rx_pos = 0;
                cdc_rx_buffer[0] = '\0';
            }
        }
    }
}

void tud_cdc_line_state_cb(uint8_t itf, bool dtr, bool rts)
{
    (void) itf;
    (void) dtr; 
    (void) rts;
    
    ESP_LOGI(TAG, "CDC line state changed: DTR=%d, RTS=%d", dtr, rts);
}

void tud_cdc_line_coding_cb(uint8_t itf, cdc_line_coding_t const* p_line_coding)
{
    (void) itf;
    ESP_LOGI(TAG, "CDC line coding: %" PRIu32 " bps, %d stop bits, %d parity, %d data bits", 
             p_line_coding->bit_rate, p_line_coding->stop_bits, 
             p_line_coding->parity, p_line_coding->data_bits);
}
#endif

#if (CFG_TUD_VIDEO)
//--------------------------------------------------------------------+
// USB Video
//--------------------------------------------------------------------+
static void video_task(void *arg)
{
    uint32_t start_ms = 0;
    uint32_t frame_num = 0;
    uint32_t frame_len = 0;
    uint32_t already_start = 0;
    uint32_t tx_busy = 0;
    uint8_t *uvc_buffer = s_uvc_device.user_config[0].uvc_buffer;
    uint32_t uvc_buffer_size = s_uvc_device.user_config[0].uvc_buffer_size;
    uvc_fb_t *pic = NULL;

    while (1)
    {
        if (!tud_video_n_streaming(0, 0))
        {
            already_start = 0;
            frame_num = 0;
            tx_busy = 0;
            vTaskDelay(1);
            continue;
        }

        if (!already_start)
        {
            already_start = 1;
            start_ms = get_time_millis();
        }

        uint32_t cur = get_time_millis();
        if (cur - start_ms < s_uvc_device.interval_ms[0])
        {
            vTaskDelay(1);
            continue;
        }

        if (tx_busy)
        {
            uint32_t xfer_done = ulTaskNotifyTake(pdTRUE, 1);
            if (xfer_done == 0)
            {
                continue;
            }
            ++frame_num;
            tx_busy = 0;
        }

        start_ms += s_uvc_device.interval_ms[0];
        ESP_LOGD(TAG, "frame %" PRIu32 " taking picture...", frame_num);
        pic = s_uvc_device.user_config[0].fb_get_cb(s_uvc_device.user_config[0].cb_ctx);
        if (pic)
        {
            ESP_LOGD(TAG, "Picture taken! Its size was: %zu bytes", pic->len);
        }
        else
        {
            ESP_LOGE(TAG, "Failed to capture picture");
            continue;
        }

        if (pic->len > uvc_buffer_size)
        {
            ESP_LOGW(TAG, "frame size is too big, dropping frame");
            s_uvc_device.user_config[0].fb_return_cb(pic, s_uvc_device.user_config[0].cb_ctx);
            continue;
        }
        frame_len = pic->len;
        memcpy(uvc_buffer, pic->buf, frame_len);
        s_uvc_device.user_config[0].fb_return_cb(pic, s_uvc_device.user_config[0].cb_ctx);
        tx_busy = 1;
        tud_video_n_frame_xfer(0, 0, (void *)uvc_buffer, frame_len);
        ESP_LOGD(TAG, "frame %" PRIu32 " transfer start, size %" PRIu32, frame_num, frame_len);
    }
}

#if CONFIG_UVC_SUPPORT_TWO_CAM
static void video_task2(void *arg)
{
    uint32_t start_ms = 0;
    uint32_t frame_num = 0;
    uint32_t frame_len = 0;
    uint32_t already_start = 0;
    uint32_t tx_busy = 0;
    uint8_t *uvc_buffer = s_uvc_device.user_config[1].uvc_buffer;
    uint32_t uvc_buffer_size = s_uvc_device.user_config[1].uvc_buffer_size;
    uvc_fb_t *pic = NULL;

    while (1)
    {
        if (!tud_video_n_streaming(1, 0))
        {
            already_start = 0;
            frame_num = 0;
            tx_busy = 0;
            vTaskDelay(1);
            continue;
        }

        if (!already_start)
        {
            already_start = 1;
            start_ms = get_time_millis();
        }

        uint32_t cur = get_time_millis();
        if (cur - start_ms < s_uvc_device.interval_ms[1])
        {
            vTaskDelay(1);
            continue;
        }

        if (tx_busy)
        {
            uint32_t xfer_done = ulTaskNotifyTake(pdTRUE, 1);
            if (xfer_done == 0)
            {
                continue;
            }
            ++frame_num;
            tx_busy = 0;
        }

        start_ms += s_uvc_device.interval_ms[1];
        ESP_LOGD(TAG, "frame %" PRIu32 " taking picture...", frame_num);
        pic = s_uvc_device.user_config[1].fb_get_cb(s_uvc_device.user_config[1].cb_ctx);
        if (pic)
        {
            ESP_LOGD(TAG, "Picture taken! Its size was: %zu bytes", pic->len);
        }
        else
        {
            ESP_LOGE(TAG, "Failed to capture picture");
            continue;
        }

        if (pic->len > uvc_buffer_size)
        {
            ESP_LOGW(TAG, "frame size is too big, dropping frame");
            s_uvc_device.user_config[1].fb_return_cb(pic, s_uvc_device.user_config[1].cb_ctx);
            continue;
        }
        frame_len = pic->len;
        memcpy(uvc_buffer, pic->buf, frame_len);
        s_uvc_device.user_config[1].fb_return_cb(pic, s_uvc_device.user_config[1].cb_ctx);
        tx_busy = 1;
        tud_video_n_frame_xfer(1, 0, (void *)uvc_buffer, frame_len);
        ESP_LOGD(TAG, "frame %" PRIu32 " transfer start, size %" PRIu32, frame_num, frame_len);
    }
}
#endif

void tud_video_frame_xfer_complete_cb(uint_fast8_t ctl_idx, uint_fast8_t stm_idx)
{
    (void)ctl_idx;
    (void)stm_idx;
    xTaskNotifyGive(s_uvc_device.uvc_task_hdl[ctl_idx]);
}

int tud_video_commit_cb(uint_fast8_t ctl_idx, uint_fast8_t stm_idx,
                        video_probe_and_commit_control_t const *parameters)
{
    (void)ctl_idx;
    (void)stm_idx;
    /* convert unit to ms from 100 ns */
    ESP_LOGI(TAG, "bFrameIndex: %u", parameters->bFrameIndex);
    ESP_LOGI(TAG, "dwFrameInterval: %" PRIu32 "", parameters->dwFrameInterval);
    if (parameters->bFrameIndex > UVC_FRAME_NUM)
    {
        return VIDEO_ERROR_OUT_OF_RANGE;
    }
    s_uvc_device.interval_ms[ctl_idx] = parameters->dwFrameInterval / 10000;
    int frame_index = parameters->bFrameIndex - 1;
    esp_err_t ret = s_uvc_device.user_config[ctl_idx].start_cb(s_uvc_device.format[ctl_idx], UVC_FRAMES_INFO[ctl_idx][frame_index].width,
                                                               UVC_FRAMES_INFO[ctl_idx][frame_index].height, UVC_FRAMES_INFO[ctl_idx][frame_index].rate, s_uvc_device.user_config[ctl_idx].cb_ctx);

    if (ret != ESP_OK)
    {
        ESP_LOGE(TAG, "camera init failed");
        return VIDEO_ERROR_OUT_OF_RANGE;
    }
    return VIDEO_ERROR_NONE;
}
#endif

esp_err_t uvc_device_config(int index, uvc_device_config_t *config)
{
    ESP_RETURN_ON_FALSE(index < UVC_CAM_NUM, ESP_ERR_INVALID_ARG, TAG, "index is invalid");
    ESP_RETURN_ON_FALSE(config != NULL, ESP_ERR_INVALID_ARG, TAG, "config is NULL");
    ESP_RETURN_ON_FALSE(config->start_cb != NULL, ESP_ERR_INVALID_ARG, TAG, "start_cb is NULL");
    ESP_RETURN_ON_FALSE(config->fb_get_cb != NULL, ESP_ERR_INVALID_ARG, TAG, "fb_get_cb is NULL");
    ESP_RETURN_ON_FALSE(config->fb_return_cb != NULL, ESP_ERR_INVALID_ARG, TAG, "fb_return_cb is NULL");
    ESP_RETURN_ON_FALSE(config->stop_cb != NULL, ESP_ERR_INVALID_ARG, TAG, "stop_cb is NULL");
    ESP_RETURN_ON_FALSE(config->uvc_buffer != NULL, ESP_ERR_INVALID_ARG, TAG, "uvc_buffer is NULL");
    ESP_RETURN_ON_FALSE(config->uvc_buffer_size > 0, ESP_ERR_INVALID_ARG, TAG, "uvc_buffer_size is 0");

    s_uvc_device.user_config[index] = *config;
    s_uvc_device.interval_ms[index] = 1000 / (index == 0 ? UVC_CAM1_FRAME_RATE : UVC_CAM2_FRAME_RATE);
    s_uvc_device.uvc_init[index] = true;
    return ESP_OK;
}

esp_err_t uvc_device_init(void)
{
    ESP_RETURN_ON_FALSE(s_uvc_device.uvc_init[0], ESP_ERR_INVALID_STATE, TAG, "uvc device 0 not init");
#if CONFIG_UVC_SUPPORT_TWO_CAM
    ESP_RETURN_ON_FALSE(s_uvc_device.uvc_init[1], ESP_ERR_INVALID_STATE, TAG, "uvc device 1 not init, if not use, please disable CONFIG_UVC_SUPPORT_TWO_CAM");
#endif

#ifdef CONFIG_FORMAT_MJPEG_CAM1
    s_uvc_device.format[0] = UVC_FORMAT_JPEG;
#endif

#if CONFIG_UVC_SUPPORT_TWO_CAM
#ifdef CONFIG_FORMAT_MJPEG_CAM2
    s_uvc_device.format[1] = UVC_FORMAT_JPEG;
#endif
#endif

    // init device stack on configured roothub port
    usb_phy_init();
    bool usb_init = tusb_init();
    if (!usb_init)
    {
        ESP_LOGE(TAG, "USB Device Stack Init Fail");
        return ESP_FAIL;
    }

    BaseType_t core_id = (CONFIG_UVC_TINYUSB_TASK_CORE < 0) ? tskNO_AFFINITY : CONFIG_UVC_TINYUSB_TASK_CORE;
    xTaskCreatePinnedToCore(tusb_device_task, "TinyUSB", 4096, NULL, CONFIG_UVC_TINYUSB_TASK_PRIORITY, NULL, core_id);
#if (CFG_TUD_VIDEO)
    core_id = (CONFIG_UVC_CAM1_TASK_CORE < 0) ? tskNO_AFFINITY : CONFIG_UVC_CAM1_TASK_CORE;
    xTaskCreatePinnedToCore(video_task, "UVC", 4096, NULL, CONFIG_UVC_CAM1_TASK_PRIORITY, &s_uvc_device.uvc_task_hdl[0], core_id);
#if CONFIG_UVC_SUPPORT_TWO_CAM
    core_id = (CONFIG_UVC_CAM2_TASK_CORE < 0) ? tskNO_AFFINITY : CONFIG_UVC_CAM2_TASK_CORE;
    xTaskCreatePinnedToCore(video_task2, "UVC2", 4096, NULL, CONFIG_UVC_CAM2_TASK_PRIORITY, &s_uvc_device.uvc_task_hdl[1], core_id);
#endif
#endif
    ESP_LOGI(TAG, "UVC Device Start, Version: %d.%d.%d", USB_DEVICE_UVC_VER_MAJOR, USB_DEVICE_UVC_VER_MINOR, USB_DEVICE_UVC_VER_PATCH);
    return ESP_OK;
}
