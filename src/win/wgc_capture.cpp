#include "win/wgc_capture.hpp"

#include <d3d11.h>
#include <dxgi1_2.h>
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

struct ScopedComInit {
    HRESULT hr = E_FAIL;
    ScopedComInit() {
        hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    }
    ~ScopedComInit() {
        if (SUCCEEDED(hr)) {
            CoUninitialize();
        }
    }
    ScopedComInit(const ScopedComInit&) = delete;
    ScopedComInit& operator=(const ScopedComInit&) = delete;
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
    HWND owner_hwnd = nullptr;
    HWND target_hwnd = nullptr;
    winrt::com_ptr<ID3D11Device> d3d_device;
    winrt::com_ptr<ID3D11DeviceContext> d3d_context;
    wgd3d::IDirect3DDevice winrt_device{nullptr};
    wgc::GraphicsCaptureItem item{nullptr};
    wgc::Direct3D11CaptureFramePool frame_pool{nullptr};
    wgc::GraphicsCaptureSession session{nullptr};
    wgc::Direct3D11CaptureFrame latest_frame{nullptr};
    winrt::Windows::Graphics::SizeInt32 last_content_size{};
    winrt::event_token frame_arrived_token{};
    winrt::event_token closed_token{};
    std::atomic<bool> target_closed{false};
    std::atomic<bool> active{false};

    void release_capture_session_locked() {
        if (latest_frame) {
            latest_frame.Close();
            latest_frame = nullptr;
        }
        if (frame_pool) {
            frame_pool.FrameArrived(frame_arrived_token);
        }
        if (item) {
            item.Closed(closed_token);
        }
        if (session) {
            session.Close();
            session = nullptr;
        }
        if (frame_pool) {
            frame_pool.Close();
            frame_pool = nullptr;
        }
        item = nullptr;
        target_hwnd = nullptr;
        last_content_size = {};
        target_closed.store(false);
    }
};

WgcCaptureSource::WgcCaptureSource() : impl_(std::make_unique<Impl>()) {}

WgcCaptureSource::~WgcCaptureSource() {
    stop_capture();
}

bool WgcCaptureSource::start_focused_capture(HWND owner_hwnd) {
    stop_capture();

    if (!wgc::GraphicsCaptureSession::IsSupported()) {
        return false;
    }

    std::lock_guard lock(impl_->mutex);
    impl_->owner_hwnd = owner_hwnd;
    impl_->target_closed.store(false);
    impl_->active.store(true);
    return true;
}

void WgcCaptureSource::stop_capture() {
    std::lock_guard lock(impl_->mutex);
    impl_->active.store(false);
    impl_->release_capture_session_locked();
    impl_->owner_hwnd = nullptr;
    impl_->winrt_device = nullptr;
    impl_->d3d_context = nullptr;
    impl_->d3d_device = nullptr;
    impl_->cv.notify_all();
}

core::CapturedFrame WgcCaptureSource::capture_frame(core::ImageFormat format,
                                                    std::uint32_t max_dimension,
                                                    std::size_t max_bytes) {
    if (!impl_->active.load()) {
        return {core::CaptureStatus::Error, {}, {}, 0, 0};
    }

    HWND fg = GetForegroundWindow();
    if (fg) {
        HWND root = GetAncestor(fg, GA_ROOT);
        if (root) {
            fg = root;
        }
    }

    HWND owner = nullptr;
    {
        std::lock_guard lock(impl_->mutex);
        owner = impl_->owner_hwnd;
    }

    if (!fg || !IsWindow(fg) || !IsWindowVisible(fg) || IsIconic(fg) || fg == owner) {
        return {core::CaptureStatus::NoNewFrame, {}, {}, 0, 0};
    }

    wgc::Direct3D11CaptureFrame frame{nullptr};
    winrt::com_ptr<ID3D11Device> d3d_device;
    winrt::com_ptr<ID3D11DeviceContext> d3d_context;

    try {
        ScopedComInit com_init;

        {
            std::lock_guard lock(impl_->mutex);
            if (!impl_->active.load()) {
                return {core::CaptureStatus::Error, {}, {}, 0, 0};
            }

            if (fg != impl_->target_hwnd || impl_->target_closed.load() || !impl_->session) {
                impl_->release_capture_session_locked();

                if (!impl_->d3d_device) {
                    impl_->d3d_device = create_d3d11_device();
                    impl_->d3d_device->GetImmediateContext(impl_->d3d_context.put());
                    winrt::com_ptr<IDXGIDevice> dxgi_device = impl_->d3d_device.as<IDXGIDevice>();
                    winrt::com_ptr<::IInspectable> inspectable_device;
                    winrt::check_hresult(CreateDirect3D11DeviceFromDXGIDevice(
                        dxgi_device.get(), inspectable_device.put()));
                    impl_->winrt_device = inspectable_device.as<wgd3d::IDirect3DDevice>();
                }

                auto interop = winrt::get_activation_factory<
                    wgc::GraphicsCaptureItem, IGraphicsCaptureItemInterop>();
                wgc::GraphicsCaptureItem item{nullptr};
                if (FAILED(interop->CreateForWindow(
                        fg,
                        winrt::guid_of<wgc::GraphicsCaptureItem>(),
                        winrt::put_abi(item))) ||
                    !item) {
                    return {core::CaptureStatus::NoNewFrame, {}, {}, 0, 0};
                }

                const auto size = item.Size();
                if (size.Width <= 0 || size.Height <= 0) {
                    return {core::CaptureStatus::NoNewFrame, {}, {}, 0, 0};
                }

                auto frame_pool = wgc::Direct3D11CaptureFramePool::CreateFreeThreaded(
                    impl_->winrt_device,
                    wgdx::DirectXPixelFormat::B8G8R8A8UIntNormalized,
                    2,
                    size);
                auto session = frame_pool.CreateCaptureSession(item);

                impl_->item = std::move(item);
                impl_->frame_pool = std::move(frame_pool);
                impl_->session = std::move(session);
                impl_->target_hwnd = fg;
                impl_->last_content_size = size;
                impl_->target_closed.store(false);

                impl_->closed_token = impl_->item.Closed([this](auto const&, auto const&) {
                    impl_->target_closed.store(true);
                    impl_->cv.notify_all();
                });

                impl_->frame_arrived_token =
                    impl_->frame_pool.FrameArrived([this](wgc::Direct3D11CaptureFramePool const& sender,
                                                          auto const&) {
                        auto arrived = sender.TryGetNextFrame();
                        if (!arrived) {
                            return;
                        }
                        {
                            std::lock_guard frame_lock(impl_->mutex);
                            if (impl_->latest_frame) {
                                impl_->latest_frame.Close();
                            }
                            impl_->latest_frame = std::move(arrived);
                        }
                        impl_->cv.notify_all();
                    });

                impl_->session.StartCapture();
            }
        }

        {
            std::unique_lock lock(impl_->mutex);
            impl_->cv.wait_for(lock, std::chrono::milliseconds(500), [this]() {
                return impl_->latest_frame != nullptr ||
                       impl_->target_closed.load() ||
                       !impl_->active.load();
            });

            if (!impl_->active.load()) {
                return {core::CaptureStatus::Error, {}, {}, 0, 0};
            }
            if (impl_->target_closed.load()) {
                impl_->release_capture_session_locked();
                return {core::CaptureStatus::NoNewFrame, {}, {}, 0, 0};
            }
            if (!impl_->latest_frame) {
                return {core::CaptureStatus::NoNewFrame, {}, {}, 0, 0};
            }

            frame = std::move(impl_->latest_frame);
            impl_->latest_frame = nullptr;
            d3d_device = impl_->d3d_device;
            d3d_context = impl_->d3d_context;
        }

        const auto content_size = frame.ContentSize();
        if (content_size.Width <= 0 || content_size.Height <= 0) {
            frame.Close();
            return {core::CaptureStatus::NoNewFrame, {}, {}, 0, 0};
        }

        {
            std::lock_guard lock(impl_->mutex);
            if (impl_->frame_pool && impl_->winrt_device &&
                (content_size.Width != impl_->last_content_size.Width ||
                 content_size.Height != impl_->last_content_size.Height)) {
                impl_->frame_pool.Recreate(
                    impl_->winrt_device,
                    wgdx::DirectXPixelFormat::B8G8R8A8UIntNormalized,
                    2,
                    content_size);
                impl_->last_content_size = content_size;
            }
        }

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
        if (frame) {
            frame.Close();
        }
        return {core::CaptureStatus::Error, {}, {}, 0, 0};
    }
}

}  // namespace stealth::win
