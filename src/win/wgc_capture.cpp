#include "win/wgc_capture.hpp"

#include <d3d11.h>
#include <dxgi1_2.h>
#include <shobjidl.h>
#include <wincodec.h>
#include <windows.graphics.capture.interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <winrt/Windows.Graphics.DirectX.h>

#include <algorithm>
#include <chrono>
#include <cstring>

namespace stealth::win {
namespace {

namespace wgc = winrt::Windows::Graphics::Capture;
namespace wgdx = winrt::Windows::Graphics::DirectX;
namespace wgd3d = winrt::Windows::Graphics::DirectX::Direct3D11;

struct __declspec(uuid("A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1"))
    IDirect3DDxgiInterfaceAccess : ::IUnknown {
    virtual HRESULT __stdcall GetInterface(REFIID iid, void** object) = 0;
};

winrt::com_ptr<ID3D11Device> create_d3d11_device() {
    UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
    winrt::com_ptr<ID3D11Device> device;
    D3D_FEATURE_LEVEL level{};
    const HRESULT hr = D3D11CreateDevice(
        nullptr,
        D3D_DRIVER_TYPE_HARDWARE,
        nullptr,
        flags,
        nullptr,
        0,
        D3D11_SDK_VERSION,
        device.put(),
        &level,
        nullptr);
    if (FAILED(hr)) {
        winrt::check_hresult(D3D11CreateDevice(
            nullptr,
            D3D_DRIVER_TYPE_WARP,
            nullptr,
            flags,
            nullptr,
            0,
            D3D11_SDK_VERSION,
            device.put(),
            &level,
            nullptr));
    }
    return device;
}

std::vector<std::uint8_t> encode_bgra_with_wic(
    const std::uint8_t* bgra_pixels,
    std::uint32_t width,
    std::uint32_t height,
    std::uint32_t stride,
    core::ImageFormat format,
    std::uint32_t max_dimension) {
    winrt::com_ptr<IWICImagingFactory> factory;
    winrt::check_hresult(CoCreateInstance(
        CLSID_WICImagingFactory,
        nullptr,
        CLSCTX_INPROC_SERVER,
        IID_PPV_ARGS(factory.put())));

    winrt::com_ptr<IWICBitmap> source_bitmap;
    winrt::check_hresult(factory->CreateBitmapFromMemory(
        width,
        height,
        GUID_WICPixelFormat32bppBGRA,
        stride,
        stride * height,
        const_cast<BYTE*>(bgra_pixels),
        source_bitmap.put()));

    winrt::com_ptr<IWICBitmapSource> bitmap_to_encode = source_bitmap;
    if (max_dimension > 0 && (width > max_dimension || height > max_dimension)) {
        const double scale = static_cast<double>(max_dimension) /
                             static_cast<double>(std::max(width, height));
        const UINT dst_w = std::max<UINT>(1, static_cast<UINT>(width * scale));
        const UINT dst_h = std::max<UINT>(1, static_cast<UINT>(height * scale));
        winrt::com_ptr<IWICBitmapScaler> scaler;
        winrt::check_hresult(factory->CreateBitmapScaler(scaler.put()));
        winrt::check_hresult(scaler->Initialize(
            source_bitmap.get(),
            dst_w,
            dst_h,
            WICBitmapInterpolationModeFant));
        bitmap_to_encode = scaler;
    }

    winrt::com_ptr<IStream> mem_stream;
    winrt::check_hresult(CreateStreamOnHGlobal(nullptr, TRUE, mem_stream.put()));

    const GUID container =
        (format == core::ImageFormat::Jpeg) ? GUID_ContainerFormatJpeg : GUID_ContainerFormatPng;
    winrt::com_ptr<IWICBitmapEncoder> encoder;
    winrt::check_hresult(factory->CreateEncoder(container, nullptr, encoder.put()));
    winrt::check_hresult(encoder->Initialize(mem_stream.get(), WICBitmapEncoderNoCache));

    winrt::com_ptr<IWICBitmapFrameEncode> frame_encode;
    winrt::com_ptr<IPropertyBag2> props;
    winrt::check_hresult(encoder->CreateNewFrame(frame_encode.put(), props.put()));
    winrt::check_hresult(frame_encode->Initialize(props.get()));

    UINT out_w = 0;
    UINT out_h = 0;
    winrt::check_hresult(bitmap_to_encode->GetSize(&out_w, &out_h));
    winrt::check_hresult(frame_encode->SetSize(out_w, out_h));

    WICPixelFormatGUID pixel_fmt = GUID_WICPixelFormat32bppBGRA;
    winrt::check_hresult(frame_encode->SetPixelFormat(&pixel_fmt));
    winrt::check_hresult(frame_encode->WriteSource(bitmap_to_encode.get(), nullptr));
    winrt::check_hresult(frame_encode->Commit());
    winrt::check_hresult(encoder->Commit());

    STATSTG stat{};
    winrt::check_hresult(mem_stream->Stat(&stat, STATFLAG_NONAME));
    const std::size_t byte_size = static_cast<std::size_t>(stat.cbSize.QuadPart);

    HGLOBAL hglobal = nullptr;
    winrt::check_hresult(GetHGlobalFromStream(mem_stream.get(), &hglobal));
    std::vector<std::uint8_t> out(byte_size);
    if (byte_size > 0) {
        void* locked = GlobalLock(hglobal);
        if (locked) {
            std::memcpy(out.data(), locked, byte_size);
            GlobalUnlock(hglobal);
        }
    }
    return out;
}

}  // namespace

struct WgcCaptureSource::Impl {
    std::mutex mutex;
    std::condition_variable cv;
    winrt::com_ptr<ID3D11Device> d3d_device;
    winrt::com_ptr<ID3D11DeviceContext> d3d_context;
    wgd3d::IDirect3DDevice winrt_device{nullptr};
    wgc::GraphicsCaptureItem item{nullptr};
    wgc::Direct3D11CaptureFramePool frame_pool{nullptr};
    wgc::GraphicsCaptureSession session{nullptr};
    wgc::Direct3D11CaptureFrame latest_frame{nullptr};
    winrt::event_token frame_arrived_token{};
    winrt::event_token closed_token{};
    std::atomic<bool> target_closed{false};
    std::atomic<bool> active{false};
};

WgcCaptureSource::WgcCaptureSource() : impl_(std::make_unique<Impl>()) {}

WgcCaptureSource::~WgcCaptureSource() {
    stop_capture();
}

bool WgcCaptureSource::pick_and_start(HWND owner_hwnd) {
    stop_capture();

    if (!wgc::GraphicsCaptureSession::IsSupported()) {
        return false;
    }

    wgc::GraphicsCapturePicker picker;
    auto init_with_window = picker.as<IInitializeWithWindow>();
    if (FAILED(init_with_window->Initialize(owner_hwnd))) {
        return false;
    }

    // Pump picker synchronously on the STA UI thread.
    auto item = picker.PickSingleItemAsync().get();
    if (!item) {
        return false;
    }

    auto d3d_device = create_d3d11_device();
    winrt::com_ptr<ID3D11DeviceContext> d3d_context;
    d3d_device->GetImmediateContext(d3d_context.put());

    winrt::com_ptr<IDXGIDevice> dxgi_device = d3d_device.as<IDXGIDevice>();
    winrt::com_ptr<::IInspectable> inspectable_device;
    winrt::check_hresult(CreateDirect3D11DeviceFromDXGIDevice(
        dxgi_device.get(), inspectable_device.put()));
    auto winrt_device = inspectable_device.as<wgd3d::IDirect3DDevice>();

    const auto size = item.Size();
    auto frame_pool = wgc::Direct3D11CaptureFramePool::CreateFreeThreaded(
        winrt_device,
        wgdx::DirectXPixelFormat::B8G8R8A8UIntNormalized,
        2,
        size);
    auto session = frame_pool.CreateCaptureSession(item);
    // Keep Windows' capture indicator enabled (default IsBorderRequired is true).

    std::lock_guard lock(impl_->mutex);
    impl_->d3d_device = std::move(d3d_device);
    impl_->d3d_context = std::move(d3d_context);
    impl_->winrt_device = std::move(winrt_device);
    impl_->item = std::move(item);
    impl_->frame_pool = std::move(frame_pool);
    impl_->session = std::move(session);
    impl_->latest_frame = nullptr;
    impl_->target_closed.store(false);
    impl_->active.store(true);

    impl_->closed_token = impl_->item.Closed([this](auto const&, auto const&) {
        impl_->target_closed.store(true);
        impl_->cv.notify_all();
    });

    impl_->frame_arrived_token =
        impl_->frame_pool.FrameArrived([this](wgc::Direct3D11CaptureFramePool const& sender,
                                              auto const&) {
            auto frame = sender.TryGetNextFrame();
            if (!frame) {
                return;
            }
            {
                std::lock_guard frame_lock(impl_->mutex);
                if (impl_->latest_frame) {
                    impl_->latest_frame.Close();
                }
                impl_->latest_frame = std::move(frame);
            }
            impl_->cv.notify_all();
        });

    impl_->session.StartCapture();
    return true;
}

void WgcCaptureSource::stop_capture() {
    std::lock_guard lock(impl_->mutex);
    impl_->active.store(false);
    if (impl_->latest_frame) {
        impl_->latest_frame.Close();
        impl_->latest_frame = nullptr;
    }
    if (impl_->frame_pool) {
        impl_->frame_pool.FrameArrived(impl_->frame_arrived_token);
    }
    if (impl_->item) {
        impl_->item.Closed(impl_->closed_token);
    }
    if (impl_->session) {
        impl_->session.Close();
        impl_->session = nullptr;
    }
    if (impl_->frame_pool) {
        impl_->frame_pool.Close();
        impl_->frame_pool = nullptr;
    }
    impl_->item = nullptr;
    impl_->winrt_device = nullptr;
    impl_->d3d_context = nullptr;
    impl_->d3d_device = nullptr;
    impl_->cv.notify_all();
}

core::CapturedFrame WgcCaptureSource::capture_frame(core::ImageFormat format,
                                                    std::uint32_t max_dimension,
                                                    std::size_t max_bytes) {
    if (impl_->target_closed.load()) {
        return {core::CaptureStatus::TargetClosed, {}, {}, 0, 0};
    }
    if (!impl_->active.load()) {
        return {core::CaptureStatus::Error, {}, {}, 0, 0};
    }

    wgc::Direct3D11CaptureFrame frame{nullptr};
    winrt::com_ptr<ID3D11Device> d3d_device;
    winrt::com_ptr<ID3D11DeviceContext> d3d_context;

    {
        std::unique_lock lock(impl_->mutex);
        impl_->cv.wait_for(lock, std::chrono::milliseconds(500), [this]() {
            return impl_->latest_frame != nullptr ||
                   impl_->target_closed.load() ||
                   !impl_->active.load();
        });

        if (impl_->target_closed.load()) {
            return {core::CaptureStatus::TargetClosed, {}, {}, 0, 0};
        }
        if (!impl_->active.load()) {
            return {core::CaptureStatus::Error, {}, {}, 0, 0};
        }
        if (!impl_->latest_frame) {
            return {core::CaptureStatus::NoNewFrame, {}, {}, 0, 0};
        }

        frame = std::move(impl_->latest_frame);
        impl_->latest_frame = nullptr;
        d3d_device = impl_->d3d_device;
        d3d_context = impl_->d3d_context;
    }

    try {
        auto surface = frame.Surface();
        auto access = surface.as<IDirect3DDxgiInterfaceAccess>();
        winrt::com_ptr<ID3D11Texture2D> src_tex;
        winrt::check_hresult(access->GetInterface(IID_PPV_ARGS(src_tex.put())));

        D3D11_TEXTURE2D_DESC desc{};
        src_tex->GetDesc(&desc);

        D3D11_TEXTURE2D_DESC staging_desc = desc;
        staging_desc.Usage = D3D11_USAGE_STAGING;
        staging_desc.BindFlags = 0;
        staging_desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        staging_desc.MiscFlags = 0;

        winrt::com_ptr<ID3D11Texture2D> staging;
        winrt::check_hresult(d3d_device->CreateTexture2D(&staging_desc, nullptr, staging.put()));
        d3d_context->CopyResource(staging.get(), src_tex.get());

        D3D11_MAPPED_SUBRESOURCE mapped{};
        winrt::check_hresult(d3d_context->Map(staging.get(), 0, D3D11_MAP_READ, 0, &mapped));

        std::vector<std::uint8_t> encoded;
        std::string mime = (format == core::ImageFormat::Jpeg) ? "image/jpeg" : "image/png";
        try {
            encoded = encode_bgra_with_wic(
                static_cast<const std::uint8_t*>(mapped.pData),
                desc.Width,
                desc.Height,
                mapped.RowPitch,
                format,
                max_dimension);

            if (encoded.size() > max_bytes && format == core::ImageFormat::Png) {
                encoded = encode_bgra_with_wic(
                    static_cast<const std::uint8_t*>(mapped.pData),
                    desc.Width,
                    desc.Height,
                    mapped.RowPitch,
                    core::ImageFormat::Jpeg,
                    max_dimension);
                mime = "image/jpeg";
            }
        } catch (...) {
            d3d_context->Unmap(staging.get(), 0);
            frame.Close();
            throw;
        }

        d3d_context->Unmap(staging.get(), 0);
        frame.Close();

        if (encoded.empty() || encoded.size() > max_bytes) {
            return {core::CaptureStatus::Error, {}, {}, 0, 0};
        }

        return core::CapturedFrame{
            .status = core::CaptureStatus::Ok,
            .image_bytes = std::move(encoded),
            .mime_type = std::move(mime),
            .width = desc.Width,
            .height = desc.Height,
        };
    } catch (...) {
        frame.Close();
        return {core::CaptureStatus::Error, {}, {}, 0, 0};
    }
}

}  // namespace stealth::win
