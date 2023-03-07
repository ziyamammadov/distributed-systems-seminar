import numpy as np
import pyopencl as cl
IS_COMPLEX = True
LOCAL_SIZE = 256
WAVE_SIZE = 32
MAX_ITERATIONS = 10
FOLDER_PATH = './kernel/complex/' if IS_COMPLEX == True else './kernel/real/'


def CG(size, non_zeros, a_values, b_values, a_pointers, a_cols, x, n_rhs, n_iterations):
    ctx = cl.create_some_context()
    queue = cl.CommandQueue(ctx)
    work_groups = 1 + ((size - 1) // LOCAL_SIZE)
    global_size = size if work_groups == 1 else work_groups * LOCAL_SIZE
    local_size = global_size if work_groups == 1 else LOCAL_SIZE
    rows_per_wg = (LOCAL_SIZE // WAVE_SIZE)
    spmv_work_groups = 1 + ((size - 1) // rows_per_wg)
    spmv_global_size = spmv_work_groups * LOCAL_SIZE
    spmv_local_size = LOCAL_SIZE
    np_type = np.dtype(np.complex64 if IS_COMPLEX else np.float32)
    val_size = np_type.itemsize
    include_file = "-I ./kernel/complex " if IS_COMPLEX else ""
    options = [
        f"{include_file} -D N_RHS={n_rhs} -D WAVE_SIZE={WAVE_SIZE} -D WG_SIZE={LOCAL_SIZE}"]

    with open(f'{FOLDER_PATH}axpy.cl', 'r') as f:
        axpy_kernel = cl.Program(ctx, f.read()).build(options=options).axpy

    with open(f'{FOLDER_PATH}aypx.cl', 'r') as f:
        aypx_kernel = cl.Program(ctx, f.read()).build(options=options).aypx

    with open(f'{FOLDER_PATH}spmv.cl', 'r') as f:
        spmv_kernel = cl.Program(ctx, f.read()).build(options=options).spmv

    with open(f'{FOLDER_PATH}sub.cl', 'r') as f:
        sub_kernel = cl.Program(ctx, f.read()).build(options=options).sub

    with open(f'{FOLDER_PATH}vdot.cl', 'r') as f:
        dot_kernel = cl.Program(ctx, f.read()).build(options=options).vdot

    # Allocate device memory and copy host arrays to device
    mf = cl.mem_flags
    int_size = np.dtype(np.int32).itemsize
    a_values_buf = cl.Buffer(
        ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, size=non_zeros * val_size, hostbuf=a_values)
    a_cols_buf = cl.Buffer(
        ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, size=non_zeros * int_size, hostbuf=a_cols)
    a_pointers_buf = cl.Buffer(
        ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, size=(size + 1) * int_size, hostbuf=a_pointers)
    b_buf = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR,
                      size=n_rhs * size * val_size, hostbuf=b_values)
    x_buf = cl.Buffer(ctx, mf.READ_WRITE | mf.COPY_HOST_PTR,
                      size=n_rhs * size * val_size, hostbuf=x)
    r_buf = cl.Buffer(ctx, mf.READ_WRITE, size=n_rhs * size * val_size)
    d_buf = cl.Buffer(ctx, mf.READ_WRITE, size=n_rhs * size * val_size)
    q_buf = cl.Buffer(ctx, mf.READ_WRITE, size=n_rhs * size * val_size)
    dot_res_buf = cl.Buffer(
        ctx, mf.READ_WRITE, size=n_rhs * work_groups * val_size)
    const_buf = cl.Buffer(ctx, mf.READ_WRITE, size=n_rhs * val_size)
    # beta_buf = cl.Buffer(ctx, mf.READ_WRITE, size=n_rhs * val_size)

    # y = A * x                   (spmv)
    spmv_kernel(queue, (spmv_global_size,), (spmv_local_size,), np.int32(size), a_values_buf,
                a_pointers_buf, a_cols_buf, x_buf, q_buf, cl.LocalMemory(n_rhs * spmv_local_size * val_size)).wait()

    # r = b - y                   (sub)
    sub_kernel(queue, (global_size,), (local_size,),
               b_buf, q_buf, r_buf, np.int32(size)).wait()
    b_buf.release()

    # d = r                       (copy)
    cl.enqueue_copy(queue, d_buf, r_buf).wait()

    # deltaNew = r^T * r               (dot)
    dot_kernel(queue, (global_size,), (local_size,), r_buf, r_buf, cl.LocalMemory(
        n_rhs * local_size * val_size), dot_res_buf, np.int32(size)).wait()
    
    h_dot_res = np.zeros(n_rhs * work_groups, dtype=np_type)
    cl.enqueue_copy(queue, h_dot_res, dot_res_buf).wait()
    delta_new = np.nan_to_num(h_dot_res, nan=0, posinf=0, neginf=0)[0]
    print(f'Delta: {delta_new}')
    for iteration in range(1):
        # q = A * d (spmv)

        spmv_kernel(queue, (spmv_global_size,), (spmv_local_size,), np.int32(size), a_values_buf,
                    a_pointers_buf, a_cols_buf, d_buf, q_buf, cl.LocalMemory(n_rhs * spmv_local_size * val_size)).wait()

        # dq = d * q(dot)
        dot_kernel(queue, (global_size,), (local_size,), d_buf, q_buf, cl.LocalMemory(
            n_rhs * local_size * val_size), dot_res_buf, np.int32(size)).wait()
        cl.enqueue_copy(queue, h_dot_res, dot_res_buf).wait()
        print(f'DQ: {h_dot_res[0]}')

        alpha = delta_new/h_dot_res[0]
        print(f'Alpha: {alpha}')

        # x = alpha * d + x(axpy)
        # alpha_buf = cl.Buffer(
        #     ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, n_rhs * val_size, hostbuf=np.complex64(alpha))
        cl.enqueue_copy(queue, const_buf, np.complex64(
            alpha), is_blocking=True).wait()

        # x = alpha * d + x(axpy)
        axpy_kernel(queue, (global_size,), (local_size,), d_buf,
                    x_buf, const_buf, np.int32(1), np.int32(size))
        cl.enqueue_copy(queue, x, x_buf).wait()
        print(f'X: {x}')

        # r = - alpha * q + r(axpy)
        axpy_kernel(queue, (global_size,), (local_size,), q_buf,
                    r_buf, const_buf, np.int32(0), np.int32(size)).wait()
        cl.enqueue_copy(queue, x, r_buf).wait()
        print(f'R: {x}')

        # deltaNew = r ^ T * r(dot)
        delta_old = delta_new
        dot_kernel(queue, (global_size,), (local_size,), r_buf, r_buf, cl.LocalMemory(
            n_rhs * local_size * val_size), dot_res_buf, np.int32(size)).wait()
        cl.enqueue_copy(queue, h_dot_res, dot_res_buf).wait()
        delta_new = h_dot_res[0]
        print(f'Delta {iteration}: {delta_new}')
        beta = delta_new/delta_old
        print(f'Beta: {beta}')

        # d = beta * d + r(aypx)
        # beta_buf = cl.Buffer(
        #     ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, n_rhs * val_size, hostbuf=np.complex64(beta))
        cl.enqueue_copy(queue, const_buf, np.complex64(
            beta), is_blocking=True).wait()
        aypx_kernel(queue, (global_size,), (local_size,), r_buf,
                    d_buf, const_buf, np.int32(size)).wait()
        cl.enqueue_copy(queue, x, d_buf).wait()
        print(f'D: {x}')
        
    cl.enqueue_copy(queue, x, x_buf).wait()
    print(f'X: {x}')
    a_values_buf.release()
    a_cols_buf.release()
    a_pointers_buf.release()
    x_buf.release()
    r_buf.release()
    q_buf.release()
    d_buf.release()
    dot_res_buf.release()
    const_buf.release()
    queue.flush()
    queue.finish()
