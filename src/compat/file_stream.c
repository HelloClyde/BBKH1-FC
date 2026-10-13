/* Read-only libretro streams using the H1 SDK's checked file ABI. */
#include "h1_sdk.h"
#include "platform/loading.h"
#include "platform/profile.h"
#include <limits.h>
#include <stdint.h>
#include <stdlib.h>
#include <streams/file_stream.h>

struct RFILE { h1_file *handle; int64_t size, position; };

void filestream_vfs_init(const struct retro_vfs_interface_info *info) { (void)info; }

RFILE *filestream_open(const char *path, unsigned mode, unsigned hints)
{
    h1_file *handle;
    RFILE *stream;
    int size;
    (void)hints;
    if (!path || mode != RETRO_VFS_FILE_ACCESS_READ) return NULL;
    handle = h1_fopen(path, "rb");
    if (!handle) return NULL;
    size = h1_fseek(handle, 0, H1_SEEK_END);
    if (size < 0 || h1_fseek(handle, 0, H1_SEEK_SET) != 0) {
        h1_fclose(handle); return NULL;
    }
    stream = (RFILE *)malloc(sizeof(*stream));
    if (!stream) { h1_fclose(handle); return NULL; }
    stream->handle = handle; stream->size = size; stream->position = 0;
    if (h1_loading_active()) h1_loading_size((size_t)size);
    return stream;
}

int64_t filestream_get_size(RFILE *stream) { return stream ? stream->size : -1; }
int64_t filestream_tell(RFILE *stream) { return stream ? stream->position : -1; }

int64_t filestream_seek(RFILE *stream, int64_t offset, int origin)
{
    int whence, result;
    if (!stream || offset < INT_MIN || offset > INT_MAX) return -1;
    if (origin == RETRO_VFS_SEEK_POSITION_START) whence = H1_SEEK_SET;
    else if (origin == RETRO_VFS_SEEK_POSITION_CURRENT) whence = H1_SEEK_CUR;
    else if (origin == RETRO_VFS_SEEK_POSITION_END) whence = H1_SEEK_END;
    else return -1;
    h1_profile_push(HP_ROM_IO);
    result = h1_fseek(stream->handle, (int)offset, whence);
    h1_profile_pop();
    if (result >= 0) stream->position = result;
    return result;
}

int64_t filestream_read(RFILE *stream, void *output, int64_t count)
{
    h1_size_t got = 0;
    if (!stream || count < 0 || count > INT_MAX) return -1;
    while (got < (h1_size_t)count) {
        h1_size_t request = (h1_size_t)count - got;
        h1_size_t chunk;
        if (request > 65536u) request = 65536u;
        h1_profile_push(HP_ROM_IO);
        chunk = h1_fread((uint8_t *)output + got, 1, request, stream->handle);
        h1_profile_pop();
        if (chunk > request) return -1;
        got += chunk;
        if (h1_loading_active()) h1_loading_read((size_t)stream->position + got);
        if (chunk != request) break;
    }
    stream->position += got;
    return got;
}

int filestream_close(RFILE *stream)
{
    int result;
    if (!stream) return -1;
    result = h1_fclose(stream->handle);
    free(stream);
    return result;
}
